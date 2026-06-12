# crick ⛓

**Proof of work that's worth the work.**

crick is a Bitcoin-style blockchain where miners earn a *difficulty discount* for
publishing improved solutions to NP-complete problems — redirecting mining energy
toward scientific computation while keeping the chain fully decentralized.

It implements the protocol from:

- *Proposal for a fully decentralized blockchain and proof-of-work algorithm for
  solving NP-complete problems* — Oliver, Ricottone, Philippopoulos, 2017
  ([arXiv:1708.09419](https://arxiv.org/abs/1708.09419))
- *Difficulty Scaling in Proof of Work for Decentralized Problem Solving* (DIPS) —
  Philippopoulos, Ricottone, Oliver, 2019
  ([arXiv:1911.00435](https://arxiv.org/abs/1911.00435))

## How it works

Every block can be mined one of two ways:

1. **Classically** — find a nonce so the block hash meets the base difficulty `d_b`
   (standard Bitcoin PoW), or
2. **With a solution** — include a *strictly better* solution to the network's
   puzzle, and the block is accepted at a reduced difficulty `d_r < d_b`.

Improving the solution is cheaper than out-hashing the network, so solving becomes
the rational mining strategy. Difficulties follow DIPS protocol **v2, "independent
updates"** (the paper's preferred scheme): `d_b` retargets after every 16 classical
blocks to hold classical production at 30 s/block, and `d_r` retargets after every
5 solution blocks to hold solution production at 60 s/block. If a full classical
window passes with no solution found, `d_r` is cut by 4× — so as the problem gets
harder, the discount deepens, and hoarding a solution risks someone else publishing
first at an ever-cheaper `d_r`. The solution window is kept small so rapid-fire
solutions drive `d_r` back up quickly (the paper's defense against the "Bubka"
hoarding attack).

### Seed problems

A network is seeded with one problem (`crick init --problem <type>`). All share
the same protocol; they differ only in the (cheap, exact, integer) verifier:

- **`docking`** (default) — given a protein and a ligand, find a ligand *pose*
  with lower energy than the best so far, scored by a fixed **integer** energy
  function on a 3D lattice. Verifying a pose is one `O(|ligand|·|protein|)` integer
  sum; finding a good one means searching rotations × translations. Easy to check,
  hard to find. Instances are (protein, ligand) pairs from a structure corpus
  (ESM Atlas) × a ligand library (Enamine).
- **`mcs-protein`** — Maximum Common Subgraph between two protein contact graphs,
  i.e. max-clique on their modular product. A solution is a residue↔residue
  mapping; its score is the number of matched residues. The chain accumulates a
  library of shared structural motifs.
- **`max-clique`** — maximum clique on a pseudorandom `G(n, p)` graph; the
  reference/benchmark problem.

All scores are integers where higher is strictly better, so "is this a real
improvement?" is an exact comparison every node agrees on — no floating point on
the consensus path. The corpus is vast enough (billions × billions of pairs) that
precomputing a meaningful fraction *is* the useful science, which is what defuses
the Bubka hoarding attack. New problems implement the `Problem` interface in
`crick/puzzle.py` and register via `@register_problem`.

> **Prototype note:** so the package runs with no external data, `docking` and
> `mcs-protein` instances are synthesised deterministically from the network seed.
> The instance spec carries the full data, so production swaps in real ESM Atlas
> contact maps / Enamine ligands by populating the same fields — the consensus
> code (verify/score) is unchanged.

## Install

Python 3.9+:

```sh
git clone https://github.com/OliverLaboratory/crick.git && cd crick
pip install .          # or: pipx install git+https://github.com/OliverLaboratory/crick.git
```

## Quickstart

```sh
crick init                          # wallet + genesis block in ~/.crick
crick mine                          # mine solo (solver + hasher in one loop)
crick node --mine --peer http://seed.example:9911   # join a network
crick status                        # height, d_b, d_r, best clique, balance
crick solution                      # best on-chain solution, verified
crick send --to crk1… --amount 5    # spend rewards via a running node
```

A node exposes a JSON API on port 9911: `GET /status`, `GET /chain`,
`GET /solution`, `POST /blocks`, `POST /tx`, `GET|POST /peers`. New blocks are
gossiped to peers; forks resolve by total accumulated work with full
re-validation.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/                 # run the test suite
.venv/bin/pytest tests/ -k retarget     # run one test
```

Package layout: `crick/params.py` (consensus constants), `puzzle.py` (the clique
problem), `block.py`, `chain.py` (validation + DIPS retargeting), `miner.py`,
`node.py` (HTTP node + gossip), `cli.py`.

The papers are in `papers/`; the project landing page is `docs/index.html`
(published via GitHub Pages from `main`/`docs`).

> ⚠️ crick is an open research prototype, not a production cryptocurrency.
