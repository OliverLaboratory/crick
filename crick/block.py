"""Blocks and transactions.

Blocks are Bitcoin-like with one addition (papers, Sec. II.A): a `solution`
field carrying a compact puzzle solution, or null when the block was mined
classically. The genesis block additionally stores the puzzle instance spec.

A block's hash is sha256d over its canonical JSON (minus the hash itself)
with the nonce appended, so miners can grind nonces without re-serializing.
"""

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .crypto import canonical_json, sha256d_hex, verify_signature, address_from_pubkey

COINBASE_SENDER = "coinbase"


@dataclass
class Transaction:
    sender: str                 # address, or "coinbase"
    recipient: str
    amount: float
    nonce: int = 0              # sender's transaction counter (replay protection)
    pubkey: str = ""            # sender's public key (empty for coinbase)
    signature: str = ""

    def payload(self) -> bytes:
        body = {"sender": self.sender, "recipient": self.recipient,
                "amount": self.amount, "nonce": self.nonce, "pubkey": self.pubkey}
        return canonical_json(body).encode()

    @property
    def txid(self) -> str:
        return sha256d_hex(self.payload() + self.signature.encode())

    @property
    def is_coinbase(self) -> bool:
        return self.sender == COINBASE_SENDER

    def verify(self) -> bool:
        if self.is_coinbase:
            return True
        if address_from_pubkey(self.pubkey) != self.sender:
            return False
        return verify_signature(self.pubkey, self.signature, self.payload())

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls(sender=d["sender"], recipient=d["recipient"], amount=float(d["amount"]),
                   nonce=int(d.get("nonce", 0)), pubkey=d.get("pubkey", ""),
                   signature=d.get("signature", ""))

    @classmethod
    def coinbase(cls, recipient: str, amount: float, height: int) -> "Transaction":
        # height in nonce keeps coinbase txids unique across blocks
        return cls(sender=COINBASE_SENDER, recipient=recipient, amount=amount, nonce=height)


@dataclass
class Block:
    height: int
    prev_hash: str
    timestamp: float
    miner: str
    difficulty: float                       # difficulty this block claims to satisfy
    transactions: List[Transaction] = field(default_factory=list)
    solution: Optional[Any] = None          # puzzle solution (any JSON value), or None
    problem: Optional[dict] = None          # genesis only: puzzle instance spec
    nonce: int = 0

    @property
    def has_solution(self) -> bool:
        return self.solution is not None

    def base_string(self) -> str:
        """Everything that is hashed, except the nonce."""
        body = {
            "height": self.height,
            "prev_hash": self.prev_hash,
            "timestamp": self.timestamp,
            "miner": self.miner,
            "difficulty": self.difficulty,
            "transactions": [t.to_dict() for t in self.transactions],
            "solution": self.solution,
            "problem": self.problem,
        }
        return canonical_json(body)

    @property
    def hash(self) -> str:
        return hash_with_nonce(self.base_string(), self.nonce)

    def to_dict(self) -> dict:
        d = {
            "height": self.height,
            "prev_hash": self.prev_hash,
            "timestamp": self.timestamp,
            "miner": self.miner,
            "difficulty": self.difficulty,
            "transactions": [t.to_dict() for t in self.transactions],
            "solution": self.solution,
            "problem": self.problem,
            "nonce": self.nonce,
            "hash": self.hash,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(
            height=int(d["height"]),
            prev_hash=d["prev_hash"],
            timestamp=float(d["timestamp"]),
            miner=d["miner"],
            difficulty=float(d["difficulty"]),
            transactions=[Transaction.from_dict(t) for t in d.get("transactions", [])],
            solution=d.get("solution"),
            problem=d.get("problem"),
            nonce=int(d.get("nonce", 0)),
        )


def hash_with_nonce(base_string: str, nonce: int) -> str:
    return sha256d_hex(f"{base_string}:{nonce}".encode())


def now() -> float:
    return time.time()
