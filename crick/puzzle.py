"""Problem interface, registry, and the shared clique search.

A crick network is seeded with one Problem. Every Problem must satisfy the
proof-of-work asymmetry the papers require and our consensus needs:

  * verify() is cheap and EXACT,
  * score() is an INTEGER where higher is strictly better (so "is this a
    real improvement?" is an unambiguous integer comparison every node agrees
    on — no floating point anywhere on the consensus path),
  * improve() is the (expensive) miner-side search; it is never trusted, only
    its output is verified.

Concrete problems: CliqueProblem here; MCSProblem and DockingProblem in
bioproblems.py. New problems register themselves with @register_problem and
are reconstructed from a block's stored spec via problem_from_spec().
"""

import hashlib
import random
from typing import Any, List, Optional

# ----------------------------------------------------------------- registry

_REGISTRY: dict = {}


def register_problem(cls):
    _REGISTRY[cls.type] = cls
    return cls


def problem_from_spec(spec: dict) -> "Problem":
    t = spec.get("type")
    if t not in _REGISTRY:
        raise ValueError(f"unknown problem type: {t!r}")
    return _REGISTRY[t].from_spec(spec)


def new_problem(problem_type: str, seed: str) -> "Problem":
    if problem_type not in _REGISTRY:
        raise ValueError(f"unknown problem type: {problem_type!r}")
    return _REGISTRY[problem_type].generate(seed)


def available_problems() -> List[str]:
    return sorted(_REGISTRY)


def seed_rng(*parts: Any) -> random.Random:
    """A deterministic RNG seeded from a SHA-256 of the given parts. Used only
    to *generate* an instance (which is then stored verbatim in the spec), so
    nodes never re-run it — they read the data."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


# ----------------------------------------------------------------- interface

class Problem:
    type = "abstract"
    SCORE_FLOOR = 0  # score(None): the bound the first solution must beat

    def spec(self) -> dict:
        raise NotImplementedError

    @classmethod
    def from_spec(cls, spec: dict) -> "Problem":
        raise NotImplementedError

    @classmethod
    def generate(cls, seed: str) -> "Problem":
        raise NotImplementedError

    def verify(self, solution: Any) -> bool:
        raise NotImplementedError

    def score(self, solution: Any) -> int:
        """Integer, higher is strictly better. score(None) == SCORE_FLOOR."""
        raise NotImplementedError

    def improve(self, best: Any, attempts: int = 200,
                rng: Optional[random.Random] = None) -> Any:
        raise NotImplementedError

    def describe(self) -> str:
        return self.type

    def summary(self, solution: Any) -> str:
        return "none yet" if solution is None else str(solution)


# ------------------------------------------------------- shared clique search

def greedy_clique_once(n: int, adjacency: List[int], rng: random.Random) -> List[int]:
    """One randomized maximal clique. `adjacency[v]` is a bitmask of v's neighbours."""
    order = list(range(n))
    rng.shuffle(order)
    candidates = (1 << n) - 1  # vertices still adjacent to all chosen
    clique: List[int] = []
    for v in order:
        if (candidates >> v) & 1:
            clique.append(v)
            candidates &= adjacency[v]
    return clique


def greedy_clique_search(n: int, adjacency: List[int], target_size: int,
                         attempts: int, rng: random.Random) -> Optional[List[int]]:
    """Randomized greedy: return a clique strictly larger than target_size, or
    None. Shared by max-clique and (via the modular product graph) MCS."""
    champion = None
    for _ in range(attempts):
        clique = greedy_clique_once(n, adjacency, rng)
        if len(clique) > target_size:
            target_size = len(clique)
            champion = sorted(clique)
    return champion


def is_clique(vertices: List[int], n: int, adjacency: List[int]) -> bool:
    if len(set(vertices)) != len(vertices):
        return False
    if any(not isinstance(v, int) or v < 0 or v >= n for v in vertices):
        return False
    for i, u in enumerate(vertices):
        for w in vertices[i + 1:]:
            if not (adjacency[u] >> w) & 1:
                return False
    return True


# ---------------------------------------------------------------- max-clique

@register_problem
class CliqueProblem(Problem):
    """Maximum clique on a pseudorandom G(n, p) graph derived from a seed.

    Retained mainly as the reference/benchmark problem; the biologically useful
    seeds are MCSProblem and DockingProblem. Verification is O(k^2) edge checks.
    """

    type = "max-clique"
    SCORE_FLOOR = 0

    def __init__(self, seed: str, n: int, p: float):
        self.seed = seed
        self.n = n
        self.p = p
        self.adjacency = build_random_graph(seed, n, p)

    def spec(self) -> dict:
        return {"type": self.type, "seed": self.seed, "n": self.n, "p": self.p}

    @classmethod
    def from_spec(cls, spec: dict) -> "CliqueProblem":
        return cls(spec["seed"], int(spec["n"]), float(spec["p"]))

    @classmethod
    def generate(cls, seed: str) -> "CliqueProblem":
        from . import params
        return cls(seed, params.GRAPH_N, params.GRAPH_P)

    def verify(self, solution: Any) -> bool:
        return bool(solution) and is_clique(solution, self.n, self.adjacency)

    def score(self, solution: Any) -> int:
        return len(solution) if solution else self.SCORE_FLOOR

    def improve(self, best, attempts: int = 200, rng=None):
        rng = rng or random.Random()
        return greedy_clique_search(self.n, self.adjacency,
                                    self.score(best), attempts, rng)

    def describe(self) -> str:
        return f"max-clique on G(n={self.n}, p={self.p})"

    def summary(self, solution) -> str:
        return "none yet" if solution is None else f"clique of size {len(solution)}"


def build_random_graph(seed: str, n: int, p: float) -> List[int]:
    adjacency = [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            digest = hashlib.sha256(f"{seed}:{i}:{j}".encode()).digest()
            if int.from_bytes(digest[:8], "big") / 2**64 < p:
                adjacency[i] |= 1 << j
                adjacency[j] |= 1 << i
    return adjacency
