"""The blockchain: validation, state, dual-difficulty retargeting, and epochs.

Two departures from Bitcoin:

1. Dual difficulty. A block is valid at the reduced difficulty d_r iff it
   carries a puzzle solution strictly better than the best recorded for the
   *active* instance; otherwise the base difficulty d_b applies. Difficulties
   follow DIPS v2 "independent updates" (arXiv:1911.00435, Sec. 2.2): d_b
   retargets from classical-block timing, d_r from solution-block timing, and a
   drought (a window of classical blocks with no solution) cuts d_r.

2. Epochs / problem rotation (the multi-puzzle scheme, 2017 Sec. III). The
   genesis block defines a corpus (problem type + seed + size), not a single
   instance. The chain works one instance at a time; when that instance
   saturates (SATURATION_WINDOW consecutive blocks find no improvement) it
   rotates to the next, whose index is `int(rotating_block_hash) % CORPUS_SIZE`
   — unpredictable until the block is mined, and not chooseable by miners.
   Per-instance bests persist (revisiting an instance must beat its history),
   and d_r resets each epoch (d_b, the global security difficulty, does not).

All epoch state is a pure function of the block sequence and params, so any
node replaying the chain reconstructs identical difficulties, the active
instance, and every per-instance best.
"""

import json
import os
from typing import Any, Dict, List, Optional

from . import bioproblems  # noqa: F401 -- registers mcs-protein and docking
from . import params
from .block import Block, now
from .puzzle import Problem, new_problem


class ValidationError(Exception):
    pass


def target_for(difficulty: float) -> int:
    return int(params.MAX_TARGET / max(difficulty, 1.0))


def meets_difficulty(block_hash: str, difficulty: float) -> bool:
    return int(block_hash, 16) < target_for(difficulty)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Blockchain:
    def __init__(self, problem_type: str, network_seed: str, corpus_size: int):
        self.problem_type = problem_type
        self.network_seed = network_seed
        self.corpus_size = corpus_size
        self.blocks: List[Block] = []
        self.balances: Dict[str, float] = {}
        self.nonces: Dict[str, int] = {}
        self.d_b: float = params.INITIAL_DIFFICULTY
        self.d_r: float = round(params.INITIAL_DIFFICULTY * params.INITIAL_ETA, 6)

        # epoch state (set when genesis is appended, updated on rotation)
        self.problem: Optional[Problem] = None   # the ACTIVE instance
        self.epoch_index: Optional[int] = None    # its index in the corpus
        self.best_solution: Any = None            # best for the active instance
        self.instance_best: Dict[int, Any] = {}   # index -> best solution (persisted)
        self.epoch_start_height: int = 0

        # difficulty / retarget counters
        self._classical_in_window = 0    # toward the next d_b retarget
        self._solutions_in_window = 0    # toward the next d_r retarget
        self._consecutive_classical = 0  # toward the next drought d_r cut
        self._dry_streak = 0             # blocks since the last improvement (rotation)
        self._anchor_b: Optional[float] = None
        self._anchor_r: Optional[float] = None

    # ------------------------------------------------------------------ build

    @classmethod
    def create(cls, network_seed: str = params.NETWORK_SEED,
               problem_type: Optional[str] = None,
               corpus_size: Optional[int] = None) -> "Blockchain":
        problem_type = problem_type or params.DEFAULT_PROBLEM
        corpus_size = corpus_size or params.CORPUS_SIZE
        # validate the type early by generating a throwaway instance
        new_problem(problem_type, f"{network_seed}:probe")
        chain = cls(problem_type, network_seed, corpus_size)
        genesis = Block(
            height=0,
            prev_hash=params.GENESIS_PREV_HASH,
            timestamp=params.GENESIS_TIMESTAMP,
            miner="genesis",
            difficulty=0.0,
            problem={"type": problem_type, "seed": network_seed, "corpus_size": corpus_size},
        )
        chain._append(genesis)
        return chain

    @classmethod
    def from_block_dicts(cls, block_dicts: List[dict]) -> "Blockchain":
        """Rebuild and fully validate a chain received from a peer or disk."""
        if not block_dicts:
            raise ValidationError("empty chain")
        genesis = Block.from_dict(block_dicts[0])
        spec = genesis.problem
        if not spec or "type" not in spec or "seed" not in spec:
            raise ValidationError("genesis missing corpus spec")
        chain = cls(spec["type"], spec["seed"],
                    int(spec.get("corpus_size", params.CORPUS_SIZE)))
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

    # --------------------------------------------------------- epoch / corpus

    def _index_from(self, block_hash: str) -> int:
        return int(block_hash, 16) % self.corpus_size

    def _instance(self, index: int) -> Problem:
        return new_problem(self.problem_type, f"{self.network_seed}:i:{index}")

    def _activate_epoch(self, index: int) -> None:
        """Make instance `index` the active problem. Restores its persisted best
        (None if never visited) and resets the per-problem difficulty d_r so a
        fresh instance can't be farmed for cheap discounted blocks."""
        self.epoch_index = index
        self.problem = self._instance(index)
        self.best_solution = self.instance_best.get(index)
        self.epoch_start_height = self.height
        self.d_r = round(max(self.d_b * params.INITIAL_ETA,
                             params.MIN_REDUCED_DIFFICULTY), 6)
        self._dry_streak = 0
        self._solutions_in_window = 0
        self._consecutive_classical = 0
        self._anchor_r = self.tip.timestamp

    def _maybe_rotate(self, block: Block) -> None:
        epoch_len = self.height - self.epoch_start_height
        if self._dry_streak >= params.SATURATION_WINDOW or epoch_len >= params.MAX_EPOCH:
            self._activate_epoch(self._index_from(block.hash))

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
        self.blocks.append(block)

        if block.height == 0:
            self._activate_epoch(self._index_from(block.hash))
            return

        if block.has_solution:
            self.best_solution = block.solution
            self.instance_best[self.epoch_index] = block.solution
        self._update_schedule(block)
        self._maybe_rotate(block)

    # -------------------------------------------------- DIPS v2 retargeting

    def _update_schedule(self, block: Block) -> None:
        if self._anchor_b is None:
            self._anchor_b = self._anchor_r = block.timestamp

        if block.has_solution:
            self._dry_streak = 0
            self._consecutive_classical = 0
            self._solutions_in_window += 1
            if self._solutions_in_window >= params.SOLUTION_WINDOW:
                self._retarget_dr(block.timestamp)
        else:
            self._dry_streak += 1
            self._classical_in_window += 1
            self._consecutive_classical += 1
            if self._classical_in_window >= params.CLASSICAL_WINDOW:
                self._retarget_db(block.timestamp)
            if self._consecutive_classical >= params.CLASSICAL_WINDOW:
                self._drought_discount()

    def _retarget_db(self, timestamp: float) -> None:
        """Hold classical block production at CLASSICAL_BLOCK_TIME (global)."""
        elapsed = max(timestamp - self._anchor_b, 0.001)
        t_star = elapsed / params.CLASSICAL_WINDOW
        x = params.MAX_RETARGET_FACTOR
        d_b_new = self.d_b * (params.CLASSICAL_BLOCK_TIME / t_star)
        d_b_new = _clamp(d_b_new, self.d_b / x, self.d_b * x)
        self.d_b = round(max(d_b_new, params.MIN_DIFFICULTY), 6)
        self._classical_in_window = 0
        self._anchor_b = timestamp

    def _retarget_dr(self, timestamp: float) -> None:
        """Hold solution block production at SOLUTION_BLOCK_TIME (per epoch).
        If a fresh instance yields easy solutions, this drives d_r back up."""
        elapsed = max(timestamp - self._anchor_r, 0.001)
        t_star = elapsed / params.SOLUTION_WINDOW
        x = params.MAX_RETARGET_FACTOR
        d_r_new = self.d_r * (params.SOLUTION_BLOCK_TIME / t_star)
        d_r_new = _clamp(d_r_new, self.d_r / x, self.d_r * x)
        self.d_r = round(max(d_r_new, params.MIN_REDUCED_DIFFICULTY), 6)
        self._solutions_in_window = 0
        self._anchor_r = timestamp

    def _drought_discount(self) -> None:
        """A full classical window with no solution cuts d_r by the max factor —
        deepening the incentive as the active instance hardens."""
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
