"""A full node: JSON-over-HTTP API, peer gossip, and an optional miner thread.

Endpoints:
  GET  /status   -> chain summary
  GET  /chain    -> full chain (blocks as JSON)
  GET  /solution -> current best puzzle solution
  POST /blocks   {"block": {...}, "from": "http://peer"} -> accept a new block
  POST /tx       {...transaction...} -> add to mempool
  GET  /peers    /  POST /peers {"url": "..."}

Fork handling is simple and safe: if an announced block does not extend our
tip, we fetch the sender's full chain and adopt it iff it shares our genesis
and carries more total work (chain.adopt_if_better re-validates everything).
"""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from .block import Block, Transaction
from .chain import Blockchain, ValidationError
from .crypto import Wallet
from .miner import mine_block


def _http_json(method: str, url: str, body: Optional[dict] = None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class Node:
    def __init__(self, chain: Blockchain, wallet: Wallet, chain_path: str,
                 host: str = "0.0.0.0", port: int = 9911,
                 public_url: Optional[str] = None):
        self.chain = chain
        self.wallet = wallet
        self.chain_path = chain_path
        self.host, self.port = host, port
        self.public_url = public_url or f"http://127.0.0.1:{port}"
        self.peers: List[str] = []
        self.mempool: List[Transaction] = []
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self._tip_hash = chain.tip.hash

    # ---------------------------------------------------------------- chain ops

    def status(self) -> dict:
        with self.lock:
            c = self.chain
            return {
                "height": c.height,
                "tip": c.tip.hash,
                "d_b": c.d_b,
                "d_r": c.d_r,
                "best_score": c.best_score,
                "total_work": c.total_work(),
                "peers": list(self.peers),
                "mempool": len(self.mempool),
                "address": self.wallet.address,
                "balance": c.balance(self.wallet.address),
            }

    def submit_block(self, block_dict: dict, sender: Optional[str] = None) -> bool:
        """Try to extend the tip; on a fork/gap, sync from the sender."""
        block = Block.from_dict(block_dict)
        with self.lock:
            try:
                self.chain.add_block(block)
                self._after_new_tip(announce=True)
                return True
            except ValidationError:
                pass
        if sender and block.height > self.chain.height:
            return self.sync_from(sender)
        return False

    def submit_tx(self, tx_dict: dict) -> bool:
        tx = Transaction.from_dict(tx_dict)
        if tx.is_coinbase or not tx.verify():
            return False
        with self.lock:
            if any(t.txid == tx.txid for t in self.mempool):
                return False
            self.mempool.append(tx)
        return True

    def sync_from(self, peer_url: str) -> bool:
        try:
            data = _http_json("GET", peer_url.rstrip("/") + "/chain")
            with self.lock:
                adopted = self.chain.adopt_if_better(data["blocks"])
                if adopted:
                    self._after_new_tip(announce=False)
                return adopted
        except (OSError, ValidationError, KeyError, ValueError):
            return False

    def _after_new_tip(self, announce: bool) -> None:
        tip = self.chain.tip
        self._tip_hash = tip.hash
        included = {t.txid for t in tip.transactions}
        self.mempool = [t for t in self.mempool if t.txid not in included]
        self.chain.save(self.chain_path)
        if announce:
            threading.Thread(target=self._broadcast, args=(tip.to_dict(),),
                             daemon=True).start()

    def _broadcast(self, block_dict: dict) -> None:
        for peer in list(self.peers):
            try:
                _http_json("POST", peer.rstrip("/") + "/blocks",
                           {"block": block_dict, "from": self.public_url})
            except OSError:
                pass

    def add_peer(self, url: str) -> None:
        url = url.rstrip("/")
        if url and url != self.public_url and url not in self.peers:
            self.peers.append(url)

    # ---------------------------------------------------------------- mining

    def _mine_loop(self, solve: bool) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                tip_at_start = self.chain.tip.hash
                mempool = list(self.mempool)
            block = mine_block(
                self.chain, self.wallet, mempool, solve=solve,
                should_abort=lambda: self.stop_event.is_set()
                or self._tip_hash != tip_at_start)
            if block is None:
                continue
            with self.lock:
                try:
                    self.chain.add_block(block)
                except ValidationError:
                    continue  # tip moved under us; restart on the new tip
                kind = (f"solution [{self.chain.problem.summary(block.solution)}]"
                        if block.has_solution else "classical")
                print(f"[mined] #{block.height} {kind} d={block.difficulty:.0f} "
                      f"{block.hash[:16]}…")
                self._after_new_tip(announce=True)

    # ---------------------------------------------------------------- serving

    def serve(self, mine: bool = False, solve: bool = True) -> None:
        node = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, obj) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length).decode() or "{}")

            def do_GET(self):
                if self.path == "/status":
                    self._send(200, node.status())
                elif self.path == "/chain":
                    with node.lock:
                        self._send(200, node.chain.to_dict())
                elif self.path == "/solution":
                    with node.lock:
                        self._send(200, {"solution": node.chain.best_solution,
                                         "score": node.chain.best_score})
                elif self.path == "/peers":
                    self._send(200, {"peers": node.peers})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                try:
                    body = self._read_body()
                except (ValueError, OSError):
                    self._send(400, {"error": "bad json"})
                    return
                if self.path == "/blocks":
                    sender = body.get("from")
                    if sender:
                        node.add_peer(sender)
                    ok = node.submit_block(body.get("block", {}), sender)
                    self._send(200 if ok else 400, {"accepted": ok})
                elif self.path == "/tx":
                    ok = node.submit_tx(body)
                    self._send(200 if ok else 400, {"accepted": ok})
                elif self.path == "/peers":
                    node.add_peer(body.get("url", ""))
                    self._send(200, {"peers": node.peers})
                else:
                    self._send(404, {"error": "not found"})

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        for peer in list(self.peers):
            try:
                _http_json("POST", peer + "/peers", {"url": self.public_url})
            except OSError:
                pass
            self.sync_from(peer)
        if mine:
            threading.Thread(target=self._mine_loop, args=(solve,), daemon=True).start()
        print(f"crick node listening on http://{self.host}:{self.port} "
              f"(height {self.chain.height}, d_b={self.chain.d_b:.0f}, "
              f"d_r={self.chain.d_r:.0f}, best clique k={self.chain.best_score})")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            server.server_close()
