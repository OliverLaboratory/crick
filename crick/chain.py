"""The blockchain: validation, state, and dual-difficulty retargeting.

The consensus-critical departure from Bitcoin: two difficulties. A block is
valid at the reduced difficulty d_r if and only if it carries a puzzle
solution strictly better than the best one recorded on its branch;
otherwise the base difficulty d_b applies.

Difficulty updates follow DIPS protocol v2, "independent updates"
(arXiv:1911.00435, Sec. 2.2):

- After every CLASSICAL_WINDOW *classical* blocks, d_b is retargeted so the
  network keeps producing classical blocks every CLASSICAL_BLOCK_TIME seconds.
- After every SOLUTION_WINDOW *solution* blocks, d_r is retargeted so
  solution blocks keep arriving every SOLUTION_BLOCK_TIME seconds.
- Drought rule: if CLASSICAL_WINDOW *consecutive* classical blocks arrive
  (no solution found), d_r is cut by MAX_RETARGET_FACTOR. As the problem
  hardens, the discount deepens — and hoarding a solution risks someone
  else publishing first at an ever-cheaper d_r.

Every update is clamped to a factor of MAX_RETARGET_FACTOR (paper eq. 2).
All schedule state (counters, window anchors) is derived purely from block
data, so any node replaying the chain arrives at the same difficulties.
"""

import json
import os
from typing import Any, Dict, List, Optional

from . import bioproblems  # noqa: F401 -- registers mcs-protein and docking
from . import params
from .block import Block, now
from .puzzle import Problem, new_problem, problem_from_spec


class ValidationError(Exception):
    pass


def target_for(difficulty: float) -> int:
    return int(params.MAX_TARGET / max(difficulty, 1.0))


def meets_difficulty(block_hash: str, difficulty: float) -> bool:
    return int(block_hash, 16) < target_for(difficulty)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Blockchain:
    def __init__(self, problem: Problem):
        self.problem = problem
        self.blocks: List[Block] = []
        self.balances: Dict[str, float] = {}
        self.nonces: Dict[str, int] = {}
        self.d_b: float = params.INITIAL_DIFFICULTY
        self.d_r: float = round(params.INITIAL_DIFFICULTY * params.INITIAL_ETA, 6)
        self.best_solution: Any = None
        # DIPS v2 schedule state, replayed deterministically from block data.
        # Anchors are the timestamps the current windows started at; they are
        # set from the first post-genesis block so the (possibly old) genesis
        # timestamp never distorts the first measurement.
        self._classical_in_window = 0
        self._solutions_in_window = 0
        self._consecutive_classical = 0
        self._anchor_b: Optional[float] = None
        self._anchor_r: Optional[float] = None

    # ------------------------------------------------------------------ build

    @classmethod
    def create(cls, network_seed: str = params.NETWORK_SEED,
               problem_type: Optional[str] = None) -> "Blockchain":
        problem = new_problem(problem_type or params.DEFAULT_PROBLEM, network_seed)
        chain = cls(problem)
        genesis = Block(
            height=0,
            prev_hash=params.GENESIS_PREV_HASH,
            timestamp=params.GENESIS_TIMESTAMP,
            miner="genesis",
            difficulty=0.0,
            problem=problem.spec(),
        )
        chain._append(genesis)
        return chain

    @classmethod
    def from_block_dicts(cls, block_dicts: List[dict]) -> "Blockchain":
        """Rebuild and fully validate a chain received from a peer or disk."""
        if not block_dicts:
            raise ValidationError("empty chain")
        genesis = Block.from_dict(block_dicts[0])
        if genesis.problem is None:
            raise ValidationError("genesis missing problem spec")
        chain = cls(problem_from_spec(genesis.problem))
        chain._append(genesis)
        for d in block_dicts[1:]:
            chain.add_block(Block.from_dict(d))
        return chain

    # ------------------------------------------------------------ chain state

    @property
    def tip(self) -> Block:
        return self.blocks[-1]

    @property
    def height(self) -> int:
        return self.tip.height

    @property
    def best_score(self) -> int:
        return self.problem.score(self.best_solution)

    def total_work(self) -> float:
        return sum(b.difficulty for b in self.blocks)

    def balance(self, address: str) -> float:
        return self.balances.get(address, 0.0)

    def next_difficulty(self, with_solution: bool) -> float:
        return self.d_r if with_solution else self.d_b

    # ------------------------------------------------------------- validation

    def add_block(self, block: Block) -> None:
        """Validate `block` as the next block and append it. Raises ValidationError."""
        tip = self.tip
        if block.height != tip.height + 1:
            raise ValidationError(f"height {block.height}, expected {tip.height + 1}")
        if block.prev_hash != tip.hash:
            raise ValidationError("prev_hash does not match tip")
        if block.problem is not None:
            raise ValidationError("only genesis may carry a problem spec")
        if block.timestamp < tip.timestamp - params.MAX_BACKWARD_DRIFT:
            raise ValidationError("timestamp too far behind parent")
        if block.timestamp > now() + params.MAX_FUTURE_DRIFT:
            raise ValidationError("timestamp too far in the future")

        # Which difficulty must this block satisfy?
        if block.has_solution:
            if not self.problem.verify(block.solution):
                raise ValidationError("invalid puzzle solution")
            if self.problem.score(block.solution) <= self.best_score:
                raise ValidationError(
                    f"solution score {self.problem.score(block.solution)} does not "
                    f"improve on best {self.best_score}")
            required = self.d_r
        else:
            required = self.d_b
        if block.difficulty != required:
            raise ValidationError(f"difficulty {block.difficulty}, expected {required}")
        if not meets_difficulty(block.hash, required):
            raise ValidationError("hash does not meet difficulty target")

        self._validate_transactions(block)
        self._append(block)

    def _validate_transactions(self, block: Block) -> None:
        txs = block.transactions
        if not txs or not txs[0].is_coinbase:
            raise ValidationError("first transaction must be coinbase")
        if any(t.is_coinbase for t in txs[1:]):
            raise ValidationError("multiple coinbase transactions")
        cb = txs[0]
        if cb.amount != params.BLOCK_REWARD or cb.nonce != block.height:
            raise ValidationError("bad coinbase")
        balances = dict(self.balances)
        nonces = dict(self.nonces)
        balances[cb.recipient] = balances.get(cb.recipient, 0.0) + cb.amount
        for tx in txs[1:]:
            if not tx.verify():
                raise ValidationError(f"bad signature on {tx.txid[:12]}")
            if tx.amount <= 0:
                raise ValidationError("non-positive amount")
            if tx.nonce != nonces.get(tx.sender, 0):
                raise ValidationError(f"bad nonce on {tx.txid[:12]}")
            if balances.get(tx.sender, 0.0) < tx.amount:
                raise ValidationError(f"insufficient funds for {tx.txid[:12]}")
            balances[tx.sender] -= tx.amount
            balances[tx.recipient] = balances.get(tx.recipient, 0.0) + tx.amount
            nonces[tx.sender] = nonces.get(tx.sender, 0) + 1

    def _append(self, block: Block) -> None:
        for tx in block.transactions:
            self.balances[tx.recipient] = self.balances.get(tx.recipient, 0.0) + tx.amount
            if not tx.is_coinbase:
                self.balances[tx.sender] -= tx.amount
                self.nonces[tx.sender] = self.nonces.get(tx.sender, 0) + 1
        if block.has_solution:
            self.best_solution = block.solution
        self.blocks.append(block)
        if block.height > 0:
            self._update_schedule(block)

    # -------------------------------------------------- DIPS v2 retargeting

    def _update_schedule(self, block: Block) -> None:
        if self._anchor_b is None:
            self._anchor_b = self._anchor_r = block.timestamp

        if block.has_solution:
            self._consecutive_classical = 0
            self._solutions_in_window += 1
            if self._solutions_in_window >= params.SOLUTION_WINDOW:
                self._retarget_dr(block.timestamp)
        else:
            self._classical_in_window += 1
            self._consecutive_classical += 1
            if self._classical_in_window >= params.CLASSICAL_WINDOW:
                self._retarget_db(block.timestamp)
            if self._consecutive_classical >= params.CLASSICAL_WINDOW:
                self._drought_discount()

    def _retarget_db(self, timestamp: float) -> None:
        """Hold classical block production at CLASSICAL_BLOCK_TIME."""
        elapsed = max(timestamp - self._anchor_b, 0.001)
        t_star = elapsed / params.CLASSICAL_WINDOW
        x = params.MAX_RETARGET_FACTOR
        d_b_new = self.d_b * (params.CLASSICAL_BLOCK_TIME / t_star)
        d_b_new = _clamp(d_b_new, self.d_b / x, self.d_b * x)
        self.d_b = round(max(d_b_new, params.MIN_DIFFICULTY), 6)
        self._classical_in_window = 0
        self._anchor_b = timestamp

    def _retarget_dr(self, timestamp: float) -> None:
        """Hold solution block production at SOLUTION_BLOCK_TIME."""
        elapsed = max(timestamp - self._anchor_r, 0.001)
        t_star = elapsed / params.SOLUTION_WINDOW
        x = params.MAX_RETARGET_FACTOR
        d_r_new = self.d_r * (params.SOLUTION_BLOCK_TIME / t_star)
        d_r_new = _clamp(d_r_new, self.d_r / x, self.d_r * x)
        self.d_r = round(max(d_r_new, params.MIN_REDUCED_DIFFICULTY), 6)
        self._solutions_in_window = 0
        self._anchor_r = timestamp

    def _drought_discount(self) -> None:
        """v2: a full classical window with no solution cuts d_r by the max
        factor, deepening the solving incentive as the problem hardens."""
        self.d_r = round(max(self.d_r / params.MAX_RETARGET_FACTOR,
                             params.MIN_REDUCED_DIFFICULTY), 6)
        self._consecutive_classical = 0

    # ------------------------------------------------------------- persistence

    def to_dict(self) -> dict:
        return {"blocks": [b.to_dict() for b in self.blocks]}

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Blockchain":
        with open(path) as f:
            data = json.load(f)
        return cls.from_block_dicts(data["blocks"])

    # ------------------------------------------------------------- fork choice

    def adopt_if_better(self, block_dicts: List[dict]) -> bool:
        """Replace our chain with a peer's if it shares our genesis and has
        more accumulated work. Returns True if adopted."""
        candidate = Blockchain.from_block_dicts(block_dicts)
        if candidate.blocks[0].hash != self.blocks[0].hash:
            raise ValidationError("different genesis")
        if candidate.total_work() <= self.total_work():
            return False
        self.__dict__.update(candidate.__dict__)
        return True
