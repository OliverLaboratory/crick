"""Biologically useful seed problems for crick.

Both satisfy the hard constraints (cheap exact verification, integer scoring,
huge unpredictable instance space):

  MCSProblem  — Maximum Common Subgraph between two protein contact graphs.
                MCS is exactly max-clique on the modular product graph, so it
                reuses the clique engine. A solution is a residue↔residue
                mapping; its score is the number of matched residues. Run over
                pairs drawn from a billions × billions structure corpus (ESM
                Atlas), the network accumulates a library of shared structural
                motifs. Verification is checking a clique in the product graph.

  DockingProblem — given a protein and a ligand, find a ligand POSE with lower
                energy than the best so far, under a fixed, fully INTEGER
                ("quantized") energy function on a 3D lattice. Verifying a pose
                is one O(|ligand|·|protein|) integer sum; finding a good pose
                means searching rotations × translations. Easy to check, hard
                to find. Instances are (protein, ligand) pairs from ESM Atlas ×
                a ligand library (Enamine) — so vast that pre-docking a
                meaningful fraction *is* the virtual-screening product, which
                neutralises the Bubka hoarding attack.

PROTOTYPE NOTE: so the package runs with no external data, instances here are
synthesised deterministically from the network seed (contact graphs / atom
clouds). The spec carries the full instance data, so a production deployment
drops in real ESM Atlas contact maps and Enamine ligands by populating the
same spec fields — no consensus code changes. The verification and scoring
paths (the only code every node runs) are pure integer arithmetic regardless.
"""

import random
from itertools import product as iproduct
from typing import Any, List, Tuple

from .puzzle import (Problem, greedy_clique_search, is_clique, register_problem,
                     seed_rng)

# ============================================================ MCS (proteins)


@register_problem
class MCSProblem(Problem):
    type = "mcs-protein"
    SCORE_FLOOR = 0

    def __init__(self, name_a: str, edges_a: List[List[int]], size_a: int,
                 name_b: str, edges_b: List[List[int]], size_b: int):
        self.name_a, self.name_b = name_a, name_b
        self.size_a, self.size_b = size_a, size_b
        self.edges_a = [tuple(e) for e in edges_a]
        self.edges_b = [tuple(e) for e in edges_b]
        self._adj_a = _adjacency_set(edges_a, size_a)
        self._adj_b = _adjacency_set(edges_b, size_b)
        # Modular product graph: vertex k <-> residue pair (i, j); edge between
        # (i,j) and (i',j') iff i!=i', j!=j' and the A-edge i~i' agrees with the
        # B-edge j~j'. A clique here is a common induced subgraph isomorphism.
        self.pairs: List[Tuple[int, int]] = list(iproduct(range(size_a), range(size_b)))
        self._index = {pair: k for k, pair in enumerate(self.pairs)}
        self.n = len(self.pairs)
        self.adjacency = self._build_product_adjacency()

    def _build_product_adjacency(self) -> List[int]:
        adj = [0] * self.n
        for k1, (i, j) in enumerate(self.pairs):
            for k2 in range(k1 + 1, self.n):
                ip, jp = self.pairs[k2]
                if i == ip or j == jp:
                    continue
                if ((ip in self._adj_a[i]) == (jp in self._adj_b[j])):
                    adj[k1] |= 1 << k2
                    adj[k2] |= 1 << k1
        return adj

    # -- consensus path (cheap, exact, integer) --

    def verify(self, solution: Any) -> bool:
        if not solution:
            return False
        try:
            vertices = [self._index[(int(i), int(j))] for i, j in solution]
        except (KeyError, TypeError, ValueError):
            return False
        return is_clique(vertices, self.n, self.adjacency)

    def score(self, solution: Any) -> int:
        return len(solution) if solution else self.SCORE_FLOOR

    # -- miner-side search --

    def improve(self, best, attempts: int = 200, rng=None):
        rng = rng or random.Random()
        clique = greedy_clique_search(self.n, self.adjacency,
                                      self.score(best), attempts, rng)
        if clique is None:
            return None
        return [list(self.pairs[k]) for k in clique]

    # -- (de)serialization & generation --

    def spec(self) -> dict:
        return {"type": self.type,
                "a": {"name": self.name_a, "size": self.size_a, "edges": [list(e) for e in self.edges_a]},
                "b": {"name": self.name_b, "size": self.size_b, "edges": [list(e) for e in self.edges_b]}}

    @classmethod
    def from_spec(cls, spec: dict) -> "MCSProblem":
        a, b = spec["a"], spec["b"]
        return cls(a["name"], a["edges"], int(a["size"]),
                   b["name"], b["edges"], int(b["size"]))

    @classmethod
    def generate(cls, seed: str) -> "MCSProblem":
        from . import params
        rng = seed_rng(seed, "mcs")
        na, ea = _synthesize_protein(rng, params.PROTEIN_RESIDUES)
        nb, eb = _synthesize_protein(rng, params.PROTEIN_RESIDUES)
        return cls(f"ESM-{rng.randrange(10**6):06d}", ea, na,
                   f"ESM-{rng.randrange(10**6):06d}", eb, nb)

    def describe(self) -> str:
        return (f"max common subgraph of proteins {self.name_a} "
                f"({self.size_a} res) and {self.name_b} ({self.size_b} res)")

    def summary(self, solution) -> str:
        return ("none yet" if solution is None
                else f"shared motif of {len(solution)} matched residues")


def _adjacency_set(edges, size: int):
    adj = [set() for _ in range(size)]
    for u, v in edges:
        adj[int(u)].add(int(v))
        adj[int(v)].add(int(u))
    return adj


def _synthesize_protein(rng: random.Random, residues: int):
    """A stand-in contact graph: the backbone chain plus a few long-range
    tertiary contacts — the shape of a real protein contact map, minus the
    biology. Replace with an ESM Atlas contact map in production."""
    edges = [[i, i + 1] for i in range(residues - 1)]
    extra = max(1, residues // 4)
    seen = set(map(tuple, edges))
    for _ in range(extra):
        i, j = sorted(rng.sample(range(residues), 2))
        if abs(i - j) > 2 and (i, j) not in seen:
            edges.append([i, j])
            seen.add((i, j))
    return residues, edges


# ================================================================== docking

# Fixed (consensus) integer energy model. All distances are squared integer
# lattice distances; all energies are integers, so every node agrees exactly.
CLASH_DIST2 = 4          # squared distance below which atoms clash
SHELL_DIST2 = 16         # squared distance up to which a contact is favourable
CLASH_PENALTY = 50       # energy added per clashing atom pair
# favourable contact energy by (ligand_type, protein_type); negative = good
CONTACT_ENERGY = [[-5, -1, 0],
                  [-1, -3, -1],
                  [0, -1, -2]]
N_ATOM_TYPES = 3


@register_problem
class DockingProblem(Problem):
    type = "docking"
    SCORE_FLOOR = -10**15  # any in-box pose beats "no pose yet"

    def __init__(self, protein_name: str, protein_atoms: List[list],
                 ligand_name: str, ligand_atoms: List[list], box: List[list]):
        self.protein_name = protein_name
        self.ligand_name = ligand_name
        # atoms: [x, y, z, type]
        self.protein = [(int(x), int(y), int(z), int(t)) for x, y, z, t in protein_atoms]
        self.ligand = [(int(x), int(y), int(z), int(t)) for x, y, z, t in ligand_atoms]
        self.box_lo = tuple(int(c) for c in box[0])
        self.box_hi = tuple(int(c) for c in box[1])

    # -- consensus path (cheap, exact, integer) --

    def verify(self, solution: Any) -> bool:
        placed = self._place(solution)
        if placed is None:
            return False
        return all(self.box_lo[d] <= a[d] <= self.box_hi[d]
                   for a in placed for d in range(3))

    def score(self, solution: Any) -> int:
        if solution is None:
            return self.SCORE_FLOOR
        placed = self._place(solution)
        if placed is None:
            return self.SCORE_FLOOR
        return -self._energy(placed)  # lower energy => higher score

    def _energy(self, placed: List[Tuple[int, int, int, int]]) -> int:
        energy = 0
        for (lx, ly, lz, lt) in placed:
            for (px, py, pz, pt) in self.protein:
                d2 = (lx - px) ** 2 + (ly - py) ** 2 + (lz - pz) ** 2
                if d2 < CLASH_DIST2:
                    energy += CLASH_PENALTY
                elif d2 <= SHELL_DIST2:
                    energy += CONTACT_ENERGY[lt][pt]
        return energy

    def _place(self, solution: Any):
        """Apply a pose {"t":[x,y,z], "rot":k} to the ligand. Rotations are
        integer matrices, so placed coordinates stay exact integers."""
        if not isinstance(solution, dict):
            return None
        try:
            t = [int(c) for c in solution["t"]]
            k = int(solution["rot"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(t) != 3 or not (0 <= k < len(ROTATIONS)):
            return None
        r = ROTATIONS[k]
        placed = []
        for (x, y, z, atype) in self.ligand:
            rx = r[0][0] * x + r[0][1] * y + r[0][2] * z + t[0]
            ry = r[1][0] * x + r[1][1] * y + r[1][2] * z + t[1]
            rz = r[2][0] * x + r[2][1] * y + r[2][2] * z + t[2]
            placed.append((rx, ry, rz, atype))
        return placed

    # -- miner-side search: random restarts + lattice hill-climb --

    def improve(self, best, attempts: int = 200, rng=None):
        rng = rng or random.Random()
        best_placed = None if best is None else self._place(best)
        best_energy = None if best_placed is None else self._energy(best_placed)
        champion, champion_energy = None, best_energy
        restarts = max(1, attempts // 20)
        for _ in range(restarts):
            pose = {"t": [rng.randint(self.box_lo[d], self.box_hi[d]) for d in range(3)],
                    "rot": rng.randrange(len(ROTATIONS))}
            placed = self._place(pose)
            energy = self._energy(placed) if (placed is not None and self.verify(pose)) else None
            pose, energy = self._hill_climb(pose, energy)
            if energy is not None and (champion_energy is None or energy < champion_energy):
                champion, champion_energy = pose, energy
        return champion

    def _hill_climb(self, pose, energy):
        improved = True
        while improved:
            improved = False
            for neighbor in self._neighbors(pose):
                placed = self._place(neighbor)
                if placed is None or not self.verify(neighbor):
                    continue
                e = self._energy(placed)
                if energy is None or e < energy:
                    pose, energy, improved = neighbor, e, True
        return pose, energy

    def _neighbors(self, pose):
        for d in range(3):
            for step in (-1, 1):
                t = list(pose["t"])
                t[d] += step
                yield {"t": t, "rot": pose["rot"]}
        for rot in (pose["rot"] - 1, pose["rot"] + 1):
            yield {"t": list(pose["t"]), "rot": rot % len(ROTATIONS)}

    # -- (de)serialization & generation --

    def spec(self) -> dict:
        return {"type": self.type,
                "protein": {"name": self.protein_name, "atoms": [list(a) for a in self.protein]},
                "ligand": {"name": self.ligand_name, "atoms": [list(a) for a in self.ligand]},
                "box": [list(self.box_lo), list(self.box_hi)]}

    @classmethod
    def from_spec(cls, spec: dict) -> "DockingProblem":
        return cls(spec["protein"]["name"], spec["protein"]["atoms"],
                   spec["ligand"]["name"], spec["ligand"]["atoms"], spec["box"])

    @classmethod
    def generate(cls, seed: str) -> "DockingProblem":
        from . import params
        rng = seed_rng(seed, "docking")
        span = params.DOCKING_BOX
        protein = [[rng.randint(0, span), rng.randint(0, span), rng.randint(0, span),
                    rng.randrange(N_ATOM_TYPES)] for _ in range(params.PROTEIN_ATOMS)]
        # small rigid ligand: a compact cluster of atoms near the origin
        ligand = [[rng.randint(-2, 2), rng.randint(-2, 2), rng.randint(-2, 2),
                   rng.randrange(N_ATOM_TYPES)] for _ in range(params.LIGAND_ATOMS)]
        box = [[-2, -2, -2], [span + 2, span + 2, span + 2]]
        return cls(f"ESM-{rng.randrange(10**6):06d}", protein,
                   f"ENA-{rng.randrange(10**8):08d}", ligand, box)

    def describe(self) -> str:
        return (f"docking ligand {self.ligand_name} ({len(self.ligand)} atoms) "
                f"into protein {self.protein_name} ({len(self.protein)} atoms)")

    def summary(self, solution) -> str:
        if solution is None:
            return "none yet"
        return f"pose with energy {-self.score(solution)} (t={solution['t']}, rot={solution['rot']})"


def _proper_rotations() -> List[List[List[int]]]:
    """The 24 integer rotation matrices of the cube (signed permutations with
    determinant +1). Integer-only, so placed coordinates are exact."""
    mats = []
    axes = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
    for perm in _permutations3():
        for sx in (1, -1):
            for sy in (1, -1):
                for sz in (1, -1):
                    col = [axes[perm[0]], axes[perm[1]], axes[perm[2]]]
                    signs = (sx, sy, sz)
                    m = [[signs[c] * col[c][r] for c in range(3)] for r in range(3)]
                    if _det3(m) == 1:
                        mats.append(m)
    return mats


def _permutations3():
    return [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)]


def _det3(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


ROTATIONS = _proper_rotations()
