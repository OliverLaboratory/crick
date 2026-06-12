"""Consensus parameters for the crick network.

crick implements DIPS protocol v2, "independent updates" (arXiv:1911.00435,
Sec. 2.2): the base difficulty d_b and the reduced difficulty d_r are
retargeted independently, each from the observed production rate of its own
block type. eta is only the *initial* ratio d_r/d_b; v2 does not enforce it.
"""

NETWORK_SEED = "crick-mainnet-v1"

# Difficulty / retargeting (DIPS v2, independent updates)
CLASSICAL_WINDOW = 16           # N2_b: classical blocks per d_b update; also the
                                # drought length that triggers the d_r decrease
SOLUTION_WINDOW = 5             # N2_r: solution blocks per d_r update — kept small
                                # so rapid-fire solutions spike d_r (Bubka defense)
CLASSICAL_BLOCK_TIME = 30.0     # t2_b: target seconds per classical block
SOLUTION_BLOCK_TIME = 60.0      # t2_r: target seconds per solution block
INITIAL_ETA = 0.5               # initial d_r/d_b only; not enforced thereafter
MAX_RETARGET_FACTOR = 4.0       # x: per-update clamp, and the drought decrease factor
INITIAL_DIFFICULTY = 1_000.0    # d_b at genesis; expected hashes per classical block
MIN_DIFFICULTY = 16.0           # floor for d_b
MIN_REDUCED_DIFFICULTY = 1.0    # floor for d_r (a solution block is never free)

MAX_TARGET = 2**256 - 1

# Economics
BLOCK_REWARD = 50.0

# Which problem the network is seeded with: "docking", "mcs-protein", or
# "max-clique" (the reference/benchmark problem).
DEFAULT_PROBLEM = "docking"

# Epoch-based problem rotation (the multi-puzzle scheme, papers Sec. III / 2.3).
# The active instance is held for an EPOCH so it gets optimized to depth, then
# rotated once it saturates (no improvement for a while). The next instance
# index is derived from the hash of the rotating block, which is both
# unpredictable (no pre-computing the corpus) and unchooseable (miners can't
# cherry-pick the easiest fresh instance). d_b (network security difficulty) is
# global and continuous across epochs; d_r (the per-problem discount) resets
# each epoch so a fresh, easily-improved instance can't be farmed for cheap
# discounted blocks before the difficulty re-adapts.
CORPUS_SIZE = 1_000_000      # number of instances the chain entropy selects among
SATURATION_WINDOW = 64       # consecutive no-improvement blocks before rotating
MAX_EPOCH = 4096             # hard cap on epoch length in blocks (backstop)

# max-clique: G(n, p) pseudorandom graph.
GRAPH_N = 120
GRAPH_P = 0.5

# mcs-protein: residues per synthesised protein contact graph (prototype;
# production loads real ESM Atlas contact maps into the spec instead).
PROTEIN_RESIDUES = 22

# docking: integer 3D lattice. Box is roughly DOCKING_BOX^3; the protein is a
# cloud of PROTEIN_ATOMS, the ligand a rigid cluster of LIGAND_ATOMS.
DOCKING_BOX = 10
PROTEIN_ATOMS = 40
LIGAND_ATOMS = 5

# Validation tolerances
MAX_FUTURE_DRIFT = 120.0        # seconds a block timestamp may lead wall clock
MAX_BACKWARD_DRIFT = 60.0       # seconds a block timestamp may lag its parent

GENESIS_TIMESTAMP = 1_750_000_000.0
GENESIS_PREV_HASH = "0" * 64
