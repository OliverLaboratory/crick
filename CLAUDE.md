# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

**crick** implements the proof-of-work blockchain described in the two papers under `papers/`. The blockchain redirects mining effort toward solving scientifically useful optimization / NP-complete problems, instead of spending it entirely on hash brute-forcing. The single-puzzle protocol is implemented as a Python package (`crick/`); the multi-puzzle extension is a later milestone.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup
.venv/bin/pytest tests/                                       # all tests
.venv/bin/pytest tests/ -k retarget                           # one test
.venv/bin/crick --data-dir /tmp/demo init                     # smoke test:
.venv/bin/crick --data-dir /tmp/demo mine --blocks 3          #   mine a few blocks
open docs/index.html                                          # landing page (Pages: main/docs)
```

There is no linter configured. The CLI entry point is `crick.cli:main` (console script `crick`).

## Code map

- `crick/params.py` — all consensus constants (windows, target times, clamp, floors, graph size). Change protocol behavior here first.
- `crick/puzzle.py` — the `Problem` interface + registry (`@register_problem`, `problem_from_spec`, `new_problem`), the shared clique search, and `CliqueProblem` (reference/benchmark). Every problem exposes `verify`/`score`/`improve`/`spec`/`from_spec`/`generate`. **Scores are integers where higher is strictly better** (so "is this an improvement?" is an exact, floating-point-free comparison); `score(None)` returns the floor the first solution must beat. `verify`/`score` are the only code every node runs — keep them cheap, exact, deterministic.
- `crick/bioproblems.py` — the two biological seed problems: `MCSProblem` (max common subgraph of protein contact graphs = clique on the modular product, reuses the clique engine) and `DockingProblem` (lowest-energy ligand pose under a fixed **integer** lattice energy function; 24 integer rotations keep placed coordinates exact). Both synthesise instances deterministically from the seed for the prototype; the spec carries full instance data so real ESM Atlas / Enamine data drops in without consensus changes. `DEFAULT_PROBLEM` (params) picks the seed; `crick init --problem` overrides.
- `crick/block.py` — `Block`/`Transaction`. A block's hash is sha256d over its canonical JSON (minus nonce/hash) with the nonce appended — see `hash_with_nonce`.
- `crick/chain.py` — the heart: validation, account-model state (balances + per-sender nonces), and the DIPS v2 independent retargeting (`_update_schedule` / `_retarget_db` / `_retarget_dr` / `_drought_discount`). Chains received from peers are fully re-validated via `from_block_dicts`; fork choice is total accumulated work (`adopt_if_better`).
- `crick/miner.py` — interleaves solver bursts with nonce chunks; rebuilds the candidate between chunks (fresh timestamp, possibly a new solution).
- `crick/node.py` — HTTP full node (stdlib only): JSON API, block gossip, sync-on-fork.
- `crick/cli.py` — `init/status/mine/node/wallet/send/solution` subcommands; data dir defaults to `~/.crick`.

Invariants to preserve when touching consensus code: retargeting must be a pure function of block data (every node replays to the same schedule — the revalidation test crosses retarget boundaries to enforce this); a solution block is valid only if its solution is *strictly* better than the branch's best; every difficulty update is clamped by `MAX_RETARGET_FACTOR` and `d_r` never falls below `MIN_REDUCED_DIFFICULTY`; keep `SOLUTION_WINDOW` small (Bubka defense). Tests in `tests/test_crick.py` shrink difficulties/windows/graph via the `fast_consensus` fixture — keep new tests fast the same way.

## Source design (the two papers)

- `papers/1708.09419v2.pdf` — *Proposal for a fully decentralized blockchain and proof-of-work algorithm for solving NP-complete problems* (Oliver, Ricottone, Philippopoulos, 2017). The original proposal.
- `papers/1911.00435v1.pdf` — *Difficulty Scaling in Proof of Work for Decentralized Problem Solving* (DIPS, 2019). Refines the difficulty-adjustment scheme and adds network simulations and attack analysis.

The 2019 (DIPS) paper supersedes the 2017 difficulty mechanism. **When the two disagree, follow DIPS.**

## Core protocol (what to implement)

Built closely on the Bitcoin blockchain, with one fundamental change to the proof-of-work:

- A block can be mined two ways:
  1. **Classically** — find a nonce `n` such that `H(B, n) < ε_d` at the network's base difficulty `d_b` (standard Bitcoin PoW).
  2. **With a solution** — include an improved solution to the network's target problem `P`, which lets the block be accepted at a *reduced* difficulty `d_r < d_b`.
- This makes solving `P` the rational, competitive thing to do: improving the solution is easier than out-hashing the network at full difficulty.
- `P` is phrased as a sequence of NP-complete **decision** problems ("does a solution with objective ≥ target exist?"), e.g. progressively larger cliques / tighter TSP tours. Solutions must be **verifiable in polynomial (constant/linear) time** with a clearly defined scoring scheme — this is the hard requirement on any problem `P`.
- **Genesis block** stores the initial problem instance (e.g. a graph in matrix form). **Subsequent blocks** store a compact solution (e.g. visited-node vector), or a null value when mined classically.

### Two difficulties, independently retargeted (DIPS v2 — what's implemented)

Maintain **two** difficulties, not one:
- `d_b` — base difficulty for blocks mined *without* a solution.
- `d_r` — reduced difficulty for blocks mined *with* a solution.

DIPS describes two update schemes. **v2, "independent updates" (Sec. 2.2), is implemented** — it's the better one (Fig. 4: solving rate independent of η; the score always saturates):
- `d_b` is retargeted after every `N₂ᵇ` **classical** blocks, to hold classical block production at `t₂ᵇ` seconds/block.
- `d_r` is retargeted after every `N₂ʳ` **solution** blocks, to hold solution production at `t₂ʳ` seconds/block.
- **Drought rule**: if `N₂ᵇ` *consecutive* classical blocks arrive (no solution found), `d_r → d_r/x`. As the problem hardens the discount deepens, and hoarding a solution risks someone else publishing first at an ever-cheaper `d_r`.
- Every update is clamped to a factor of `x` (= 4, as in Bitcoin); `η` is only the *initial* `d_r/d_b` ratio and is not enforced afterward.

(The 2017 paper / DIPS v1 "single update" scheme — joint retargeting that enforces `⟨d_r/d_b⟩ = η` — is documented in the papers but intentionally not used: under v1, `d_r` rises with `d_b` even when no solutions are found, choking the incentive.)

### Epochs / problem rotation (multi-puzzle, 2017 §III)

The genesis block defines a **corpus** (`{type, seed, corpus_size}`), not a single instance. The chain optimizes one instance per **epoch**; when it **saturates** (`SATURATION_WINDOW` consecutive blocks with no improvement) it rotates to the next instance, index = `int(rotating_block_hash) % CORPUS_SIZE`. This is deliberately **unpredictable** (no pre-computing the corpus) and **unchooseable** (miners can't cherry-pick the easiest fresh instance). Key invariants in `chain.py`:
- **`d_b` is global and continuous** across epochs (network security / hashrate). **`d_r` resets** to `d_b·η` on each rotation — so a fresh, easily-improved instance can't be farmed for cheap discounted blocks before `d_r` re-adapts. This split is the fix for "a new instance is always easier to improve."
- **Per-instance bests persist** (`instance_best[index]`): revisiting an instance must beat its recorded history, not start from zero.
- All epoch state (active instance, bests, counters, difficulties) is a pure function of the block sequence + params, so replay via `from_block_dicts` reconstructs it exactly. Tests force rotation by mining `SATURATION_WINDOW` classical blocks; keep `SATURATION_WINDOW` above any consecutive-classical run in other tests (the `fast_consensus` fixture sets it to 16).

### Multiple-puzzle extension (Section III of the 2017 paper — later phase)

A single fixed `P` exhausts its usefulness quickly. The intended end state solves a set `Ω` of problem instances:
- **Puzzle storage** — `Ω` must stay decentralized: either small enough to live in the genesis block, or held by **storage nodes** (Filecoin-style, rewarded) with the main chain holding pointers.
- **Puzzle selection** — for each block, derive the admissible puzzle index from the hash of the previous block `H(B_t)`, so the choice is deterministic and unforgeable; can be weighted to favor under-worked problems.
- **New puzzles** — added via a special, fee-bearing transaction (fee rewards miners and discourages junk problems) or via an off-chain-agreed fork/upgrade. New instances must be valid, novel, and not already solved.

Build the single-puzzle protocol first; treat multi-puzzle `Ω` as a later milestone.

### Candidate problems

Multiple sequence alignment, protein/biomolecule folding & design, Ising-lattice ground state — anything NP-complete with public datasets and fast solution verification.

### Security note (from DIPS, Sec. 4)

The main DIPS-specific attack is the **Bubka attack**: hoarding successive solutions (or copying them from solution blocks) and drip-feeding them to win many blocks in a row or fork the chain cheaply. In-protocol mitigation, which must be preserved: keep the solution window `N₂ʳ` **small**, so rapid-fire solution blocks quickly drive `d_r` back up. Social-layer mitigations (longer confirmation times, distrusting long solution-block runs) are noted in the paper but not implemented.
