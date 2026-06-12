"""The `crick` command-line interface."""

import argparse
import json
import os
import sys
import urllib.request

from . import __version__, params
from .block import Transaction
from .chain import Blockchain, ValidationError
from .crypto import Wallet
from .miner import mine_block
from .node import Node
from .puzzle import available_problems

DEFAULT_DATA_DIR = os.path.expanduser("~/.crick")


def _paths(data_dir: str):
    return os.path.join(data_dir, "chain.json"), os.path.join(data_dir, "wallet.json")


def _load_wallet(wallet_path: str) -> Wallet:
    if not os.path.exists(wallet_path):
        sys.exit(f"No wallet at {wallet_path} — run `crick init` first.")
    with open(wallet_path) as f:
        return Wallet.from_hex(json.load(f)["private_key"])


def _load_chain(chain_path: str) -> Blockchain:
    if not os.path.exists(chain_path):
        sys.exit(f"No chain at {chain_path} — run `crick init` first.")
    return Blockchain.load(chain_path)


def cmd_init(args) -> None:
    os.makedirs(args.data_dir, exist_ok=True)
    chain_path, wallet_path = _paths(args.data_dir)
    if os.path.exists(wallet_path) and not args.force:
        sys.exit(f"{wallet_path} already exists (use --force to overwrite).")
    wallet = Wallet.generate()
    with open(wallet_path, "w") as f:
        json.dump({"private_key": wallet.private_key_hex, "address": wallet.address}, f)
    os.chmod(wallet_path, 0o600)
    chain = Blockchain.create(args.seed, problem_type=args.problem,
                              manifest_url=args.manifest)
    chain.save(chain_path)
    print(f"Initialized crick node in {args.data_dir}")
    if args.manifest:
        print(f"  corpus       : {args.manifest}")
        print(f"                 ({len(chain.corpus.entries)} real proteins, "
              f"{chain.corpus_size} MCS instances)")
    else:
        print(f"  network seed : {args.seed}")
    print(f"  puzzle       : {chain.problem.describe()}")
    print(f"  your address : {wallet.address}")
    print("Next: `crick mine` (solo) or `crick node --mine --peer <url>` (network).")


def cmd_status(args) -> None:
    chain_path, wallet_path = _paths(args.data_dir)
    chain = _load_chain(chain_path)
    wallet = _load_wallet(wallet_path)
    print(f"height       : {chain.height}")
    print(f"total work   : {chain.total_work():.0f}")
    print(f"d_b (base)   : {chain.d_b:.1f}")
    print(f"d_r (reduced): {chain.d_r:.1f}  (eta = {chain.d_r / chain.d_b:.2f})")
    print(f"epoch        : instance #{chain.epoch_index} of {chain.corpus_size} "
          f"(since block {chain.epoch_start_height}; {len(chain.instance_best)} solved so far)")
    print(f"puzzle       : {chain.problem.describe()}")
    print(f"best solution: {chain.problem.summary(chain.best_solution)}")
    print(f"address      : {wallet.address}")
    print(f"balance      : {chain.balance(wallet.address):g} CRK")


def cmd_mine(args) -> None:
    chain_path, wallet_path = _paths(args.data_dir)
    chain = _load_chain(chain_path)
    wallet = _load_wallet(wallet_path)
    solve = not args.no_solve
    mined = 0
    print(f"Mining to {wallet.address} (solver {'on' if solve else 'off'}). Ctrl-C to stop.")
    try:
        while args.blocks is None or mined < args.blocks:
            block = mine_block(chain, wallet, solve=solve)
            chain.add_block(block)
            chain.save(chain_path)
            mined += 1
            kind = (f"solution [{chain.problem.summary(block.solution)}]"
                    if block.has_solution else "classical")
            print(f"#{block.height:>5}  d={block.difficulty:>8.1f}  {kind}  "
                  f"balance={chain.balance(wallet.address):g}")
    except KeyboardInterrupt:
        print(f"\nStopped. Mined {mined} block(s); chain saved to {chain_path}.")


def cmd_node(args) -> None:
    chain_path, wallet_path = _paths(args.data_dir)
    chain = _load_chain(chain_path)
    wallet = _load_wallet(wallet_path)
    node = Node(chain, wallet, chain_path, host=args.host, port=args.port,
                public_url=args.public_url)
    for peer in args.peer or []:
        node.add_peer(peer)
    node.serve(mine=args.mine, solve=not args.no_solve, explorer=args.explorer)


def cmd_wallet(args) -> None:
    _, wallet_path = _paths(args.data_dir)
    wallet = _load_wallet(wallet_path)
    print(f"address : {wallet.address}")
    if args.show_private_key:
        print(f"private : {wallet.private_key_hex}")


def cmd_send(args) -> None:
    chain_path, wallet_path = _paths(args.data_dir)
    wallet = _load_wallet(wallet_path)
    chain = _load_chain(chain_path)
    tx = Transaction(sender=wallet.address, recipient=args.to, amount=args.amount,
                     nonce=chain.nonces.get(wallet.address, 0), pubkey=wallet.pubkey)
    tx.signature = wallet.sign(tx.payload())
    url = args.node.rstrip("/") + "/tx"
    req = urllib.request.Request(url, data=json.dumps(tx.to_dict()).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
    print("accepted" if result.get("accepted") else "rejected", f"(txid {tx.txid[:16]}…)")


def cmd_solution(args) -> None:
    chain_path, _ = _paths(args.data_dir)
    chain = _load_chain(chain_path)
    print(f"puzzle   : {chain.problem.describe()}")
    print(f"best     : {chain.problem.summary(chain.best_solution)}")
    if chain.best_solution is not None:
        print(f"verified : {chain.problem.verify(chain.best_solution)}")
        print(f"solution : {json.dumps(chain.best_solution)}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="crick",
        description="A proof-of-work blockchain that mines solutions to NP-complete problems.")
    parser.add_argument("--version", action="version", version=f"crick {__version__}")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help=f"data directory (default: {DEFAULT_DATA_DIR})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a wallet and the genesis block")
    p.add_argument("--seed", default=params.NETWORK_SEED, help="network seed")
    p.add_argument("--problem", default=params.DEFAULT_PROBLEM, choices=available_problems(),
                   help=f"seed problem (default: {params.DEFAULT_PROBLEM})")
    p.add_argument("--manifest", default=None,
                   help="URL of a real-data corpus manifest (pins its hash in genesis); "
                        "overrides --problem")
    p.add_argument("--force", action="store_true", help="overwrite an existing wallet")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="show chain and wallet status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("mine", help="mine blocks solo (no networking)")
    p.add_argument("--blocks", type=int, default=None, help="stop after this many blocks")
    p.add_argument("--no-solve", action="store_true",
                   help="mine classically only (skip the puzzle solver)")
    p.set_defaults(func=cmd_mine)

    p = sub.add_parser("node", help="run a full node (optionally mining)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9911)
    p.add_argument("--peer", action="append", help="peer URL (repeatable)")
    p.add_argument("--public-url", default=None,
                   help="URL peers should use to reach this node")
    p.add_argument("--mine", action="store_true", help="mine while serving")
    p.add_argument("--no-solve", action="store_true")
    p.add_argument("--explorer", action="store_true",
                   help="serve the web block explorer at /")
    p.set_defaults(func=cmd_node)

    p = sub.add_parser("wallet", help="show wallet info")
    p.add_argument("--show-private-key", action="store_true")
    p.set_defaults(func=cmd_wallet)

    p = sub.add_parser("send", help="send CRK via a running node")
    p.add_argument("--to", required=True)
    p.add_argument("--amount", type=float, required=True)
    p.add_argument("--node", default="http://127.0.0.1:9911")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("solution", help="show the best puzzle solution on-chain")
    p.set_defaults(func=cmd_solution)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ValidationError as e:
        sys.exit(f"validation error: {e}")


if __name__ == "__main__":
    main()
