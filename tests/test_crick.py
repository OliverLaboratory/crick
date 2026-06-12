import pytest

from crick import params
from crick.block import Block, Transaction
from crick.chain import Blockchain, ValidationError, meets_difficulty
from crick.crypto import Wallet, verify_signature
from crick.miner import mine_block
from crick.puzzle import CliqueProblem, new_problem


@pytest.fixture(autouse=True)
def fast_consensus(monkeypatch):
    """Tiny difficulties and small instances so tests mine in milliseconds."""
    monkeypatch.setattr(params, "INITIAL_DIFFICULTY", 8.0)
    monkeypatch.setattr(params, "MIN_DIFFICULTY", 2.0)
    monkeypatch.setattr(params, "GRAPH_N", 30)
    monkeypatch.setattr(params, "CLASSICAL_WINDOW", 6)
    monkeypatch.setattr(params, "SOLUTION_WINDOW", 2)
    monkeypatch.setattr(params, "SATURATION_WINDOW", 16)
    monkeypatch.setattr(params, "PROTEIN_RESIDUES", 14)
    monkeypatch.setattr(params, "PROTEIN_ATOMS", 20)
    monkeypatch.setattr(params, "LIGAND_ATOMS", 4)
    monkeypatch.setattr(params, "DOCKING_BOX", 8)


def grind(block):
    """Find a valid nonce for a hand-built block (tiny difficulties)."""
    while not meets_difficulty(block.hash, block.difficulty):
        block.nonce += 1
    return block


@pytest.fixture
def chain():
    return Blockchain.create("test-seed", problem_type="max-clique")


@pytest.fixture
def wallet():
    return Wallet.generate()


# ----------------------------------------------------------------- crypto

def test_sign_and_verify(wallet):
    msg = b"hello crick"
    sig = wallet.sign(msg)
    assert verify_signature(wallet.pubkey, sig, msg)
    assert not verify_signature(wallet.pubkey, sig, b"tampered")


# ----------------------------------------------------------------- puzzle

def test_graph_is_deterministic():
    a = CliqueProblem("seed", 20, 0.5)
    b = CliqueProblem("seed", 20, 0.5)
    c = CliqueProblem("other", 20, 0.5)
    assert a.adjacency == b.adjacency
    assert a.adjacency != c.adjacency


def test_verify_rejects_non_cliques():
    p = CliqueProblem("seed", 20, 0.5)
    found = p.improve(None, attempts=50)
    assert found and p.verify(found)
    assert not p.verify([])
    assert not p.verify([0, 0])           # duplicates
    assert not p.verify([0, 99])          # out of range
    non_edge = next(([i, j] for i in range(20) for j in range(i + 1, 20)
                     if not (p.adjacency[i] >> j) & 1), None)
    assert non_edge is not None and not p.verify(non_edge)


def test_solver_improves_monotonically():
    p = CliqueProblem("seed", 30, 0.6)
    best = p.improve(None, attempts=100)
    better = p.improve(best, attempts=200)
    if better is not None:
        assert len(better) > len(best)
        assert p.verify(better)


# ------------------------------------------------------------------ blocks

def test_block_serialization_roundtrip(chain, wallet):
    block = mine_block(chain, wallet, solve=False)
    restored = Block.from_dict(block.to_dict())
    assert restored.hash == block.hash


def test_nonce_changes_hash(chain, wallet):
    block = mine_block(chain, wallet, solve=False)
    h1 = block.hash
    block.nonce += 1
    assert block.hash != h1


# ------------------------------------------------------------------- chain

def test_genesis(chain):
    assert chain.height == 0
    assert chain.tip.problem["type"] == "max-clique"
    assert chain.d_r == pytest.approx(chain.d_b * params.INITIAL_ETA)


def test_mine_classical_and_solution_blocks(chain, wallet):
    classical = mine_block(chain, wallet, solve=False)
    assert not classical.has_solution
    assert classical.difficulty == chain.d_b
    chain.add_block(classical)

    with_solution = mine_block(chain, wallet, solve=True)
    assert with_solution.has_solution  # tiny graph: solver always finds a first clique
    assert with_solution.difficulty == chain.d_r
    chain.add_block(with_solution)

    assert chain.best_score == len(with_solution.solution)
    assert chain.balance(wallet.address) == 2 * params.BLOCK_REWARD


def test_rejects_non_improving_solution(chain, wallet):
    chain.add_block(mine_block(chain, wallet, solve=True))
    stale = chain.best_solution
    block = mine_block(chain, wallet, solve=False)
    block.solution = stale  # same score as best: must be rejected
    block.difficulty = chain.d_r
    with pytest.raises(ValidationError):
        chain.add_block(block)


def test_rejects_wrong_difficulty(chain, wallet):
    block = mine_block(chain, wallet, solve=False)
    block.difficulty = chain.d_r  # classical block claiming reduced difficulty
    with pytest.raises(ValidationError):
        chain.add_block(block)


def test_rejects_bad_pow(chain, wallet):
    block = mine_block(chain, wallet, solve=False)
    while meets_difficulty(block.hash, block.difficulty):
        block.nonce += 1
    with pytest.raises(ValidationError):
        chain.add_block(block)


def test_transactions_and_balances(chain, wallet):
    recipient = Wallet.generate()
    chain.add_block(mine_block(chain, wallet, solve=False))
    tx = Transaction(sender=wallet.address, recipient=recipient.address,
                     amount=10.0, nonce=0, pubkey=wallet.pubkey)
    tx.signature = wallet.sign(tx.payload())
    chain.add_block(mine_block(chain, wallet, mempool=[tx], solve=False))
    assert chain.balance(recipient.address) == 10.0
    assert chain.balance(wallet.address) == 2 * params.BLOCK_REWARD - 10.0

    # replaying the same nonce must fail
    replay = Transaction.from_dict(tx.to_dict())
    block = mine_block(chain, wallet, mempool=[replay], solve=False)
    with pytest.raises(ValidationError):
        chain.add_block(block)


def test_rejects_overspend(chain, wallet):
    chain.add_block(mine_block(chain, wallet, solve=False))
    tx = Transaction(sender=wallet.address, recipient="crk1" + "0" * 40,
                     amount=params.BLOCK_REWARD + 1, nonce=0, pubkey=wallet.pubkey)
    tx.signature = wallet.sign(tx.payload())
    other_miner = Wallet.generate()  # so the sender gets no coinbase this block
    block = mine_block(chain, other_miner, mempool=[tx], solve=False)
    with pytest.raises(ValidationError):
        chain.add_block(block)


def test_db_retargets_after_classical_window(chain, wallet):
    d_b_before = chain.d_b
    for _ in range(params.CLASSICAL_WINDOW):
        chain.add_block(mine_block(chain, wallet, solve=False))
    # blocks arrive nearly instantly, so d_b rises by exactly the clamp factor
    assert chain.d_b == pytest.approx(d_b_before * params.MAX_RETARGET_FACTOR)


def test_drought_cuts_dr_by_max_factor(chain, wallet):
    d_r_before = chain.d_r
    for _ in range(params.CLASSICAL_WINDOW):
        chain.add_block(mine_block(chain, wallet, solve=False))
    expected = max(d_r_before / params.MAX_RETARGET_FACTOR,
                   params.MIN_REDUCED_DIFFICULTY)
    assert chain.d_r == pytest.approx(expected)
    # a second full drought window cuts it again (down to the floor)
    d_r_mid = chain.d_r
    for _ in range(params.CLASSICAL_WINDOW):
        chain.add_block(mine_block(chain, wallet, solve=False))
    assert chain.d_r == pytest.approx(
        max(d_r_mid / params.MAX_RETARGET_FACTOR, params.MIN_REDUCED_DIFFICULTY))


def test_solution_resets_drought_counter(chain, wallet):
    d_r_before = chain.d_r
    for _ in range(params.CLASSICAL_WINDOW - 1):
        chain.add_block(mine_block(chain, wallet, solve=False))
    chain.add_block(_solution_block(chain, wallet, _improving_solution(chain)))
    chain.add_block(mine_block(chain, wallet, solve=False))
    # the drought never completed: d_r was not discounted
    assert chain.d_r == pytest.approx(d_r_before)


def test_dr_retargets_after_solution_window(chain, wallet):
    d_r_before = chain.d_r
    for _ in range(params.SOLUTION_WINDOW):
        chain.add_block(_solution_block(chain, wallet, _improving_solution(chain)))
    # solutions arrive nearly instantly, so d_r rises by exactly the clamp factor
    assert chain.d_r == pytest.approx(d_r_before * params.MAX_RETARGET_FACTOR)
    assert chain.d_b == pytest.approx(8.0)  # d_b untouched: updates are independent


def _improving_solution(chain):
    """A clique exactly one larger than the chain's best (clique prefixes are
    cliques, so we extend a reference clique one vertex at a time)."""
    reference = chain.problem.improve(None, attempts=500)
    k = chain.best_score + 1
    assert len(reference) >= k, "reference clique too small for another improvement"
    return sorted(reference[:k])


def _solution_block(chain, wallet, solution):
    from crick.miner import build_candidate
    block = build_candidate(chain, wallet, solve=False)
    block.solution = solution
    block.difficulty = chain.d_r
    return grind(block)


def test_full_chain_revalidation_and_fork_choice(chain, wallet):
    # cross a d_b retarget and a drought discount so revalidation must
    # reproduce the whole difficulty schedule, not just the initial values
    for _ in range(params.CLASSICAL_WINDOW + 2):
        chain.add_block(mine_block(chain, wallet, solve=False))
    restored = Blockchain.from_block_dicts([b.to_dict() for b in chain.blocks])
    assert restored.tip.hash == chain.tip.hash
    assert restored.total_work() == chain.total_work()

    # a node behind the tip adopts the heavier chain; the reverse is refused
    short = Blockchain.from_block_dicts([b.to_dict() for b in chain.blocks[:2]])
    assert short.adopt_if_better([b.to_dict() for b in chain.blocks])
    assert short.tip.hash == chain.tip.hash
    assert not chain.adopt_if_better([b.to_dict() for b in chain.blocks[:2]])


def test_persistence_roundtrip(tmp_path, chain, wallet):
    chain.add_block(mine_block(chain, wallet))
    path = str(tmp_path / "chain.json")
    chain.save(path)
    loaded = Blockchain.load(path)
    assert loaded.tip.hash == chain.tip.hash
    assert loaded.balances == chain.balances


# ------------------------------------------------------- biological problems

def test_mcs_verify_and_score():
    p = new_problem("mcs-protein", "seed-x")
    sol = p.improve(None, attempts=300)
    assert sol and p.verify(sol)
    assert p.score(sol) == len(sol)
    assert p.score(None) == 0
    # a fabricated mapping that isn't a clique in the product graph is rejected
    assert not p.verify(sol + [[sol[0][0], sol[1][1]]])  # reuses a residue index
    # solutions are residue↔residue pairs within both proteins' ranges
    for i, j in sol:
        assert 0 <= i < p.size_a and 0 <= j < p.size_b


def test_docking_is_deterministic_and_exact():
    p = new_problem("docking", "seed-y")
    q = new_problem("docking", "seed-y")
    pose = p.improve(None, attempts=400)
    assert pose and p.verify(pose)
    # identical instance + identical pose => identical integer score on any node
    assert p.score(pose) == q.score(pose)
    # score is the negated integer lattice energy; better pose => strictly higher
    better = p.improve(pose, attempts=800)
    if better is not None:
        assert p.score(better) > p.score(pose)
    # an out-of-box pose is invalid; a malformed pose is rejected, not crashing
    assert not p.verify({"t": [10_000, 10_000, 10_000], "rot": 0})
    assert not p.verify({"t": [0, 0], "rot": 0})
    assert not p.verify({"rot": 0})
    # "no pose yet" scores below any real pose, so the first valid pose improves
    assert p.score(None) < p.score(pose)


def test_chain_runs_on_each_problem(wallet):
    for problem_type in ("docking", "mcs-protein", "max-clique"):
        chain = Blockchain.create("seed-run", problem_type=problem_type)
        chain.add_block(mine_block(chain, wallet, solve=True))
        assert chain.height == 1
        # the genesis records the seed problem; the block carries a real solution
        assert chain.blocks[0].problem["type"] == problem_type
        sol_block = chain.blocks[1]
        assert sol_block.has_solution
        assert chain.problem.verify(sol_block.solution)
        # full re-validation from serialized form reproduces the tip
        restored = Blockchain.from_block_dicts([b.to_dict() for b in chain.blocks])
        assert restored.tip.hash == chain.tip.hash


# ------------------------------------------------------- epochs / rotation

def test_problem_rotates_on_saturation(chain, wallet):
    start_index = chain.epoch_index
    # mine the genesis solution first so the active instance is "in progress"
    chain.add_block(mine_block(chain, wallet, solve=True))
    assert chain.epoch_index == start_index
    # a full saturation window with no improvement rotates to a new instance
    for _ in range(params.SATURATION_WINDOW):
        chain.add_block(mine_block(chain, wallet, solve=False))
    assert chain.epoch_index != start_index           # rotated
    assert chain.best_solution is None                # fresh instance, nothing yet
    assert chain.epoch_start_height == chain.height    # epoch just began


def test_fresh_epoch_resets_dr(chain, wallet):
    for _ in range(params.SATURATION_WINDOW):
        chain.add_block(mine_block(chain, wallet, solve=False))
    # on rotation d_r is reset toward d_b (not left at the drought-cut value),
    # so a fresh, easily-improved instance can't be farmed at a deep discount
    expected = round(max(chain.d_b * params.INITIAL_ETA, params.MIN_REDUCED_DIFFICULTY), 6)
    assert chain.d_r == pytest.approx(expected)


def test_db_is_global_dr_is_per_epoch(chain, wallet):
    # d_b retargets from classical timing regardless of rotation; mining a full
    # saturation window (all classical) pushes d_b up and survives the rotation
    d_b_before = chain.d_b
    for _ in range(params.SATURATION_WINDOW):
        chain.add_block(mine_block(chain, wallet, solve=False))
    assert chain.d_b > d_b_before


def test_per_instance_best_persists_on_revisit(monkeypatch, wallet):
    # corpus of size 1: every rotation returns to the same instance, so its best
    # must persist — a revisited instance is not a fresh start
    monkeypatch.setattr(params, "CORPUS_SIZE", 1)
    chain = Blockchain.create("revisit", problem_type="max-clique")
    chain.add_block(mine_block(chain, wallet, solve=True))
    best_before = chain.best_score
    assert best_before > 0
    for _ in range(params.SATURATION_WINDOW):
        chain.add_block(mine_block(chain, wallet, solve=False))
    assert chain.epoch_index == 0                      # same instance
    assert chain.best_score == best_before             # best persisted, not reset


def test_epoch_rotation_deterministic_on_replay(chain, wallet):
    chain.add_block(mine_block(chain, wallet, solve=True))
    for _ in range(params.SATURATION_WINDOW):          # force a rotation
        chain.add_block(mine_block(chain, wallet, solve=False))
    chain.add_block(mine_block(chain, wallet, solve=True))  # solve the new instance
    restored = Blockchain.from_block_dicts([b.to_dict() for b in chain.blocks])
    assert restored.tip.hash == chain.tip.hash
    assert restored.epoch_index == chain.epoch_index
    assert restored.best_score == chain.best_score
    assert restored.d_b == chain.d_b and restored.d_r == chain.d_r
    assert restored.instance_best.keys() == chain.instance_best.keys()


# ----------------------------------------------------- real (manifest) corpus

def _write_corpus(tmp_path, proteins):
    """Write a content-addressed mcs-protein corpus to tmp_path; return file:// URL."""
    import hashlib
    from crick.crypto import canonical_json
    (tmp_path / "proteins").mkdir()
    entries = []
    for name, size, edges in proteins:
        blob = {"name": name, "size": size, "edges": edges}
        rel = f"proteins/{name}.json"
        (tmp_path / rel).write_text(canonical_json(blob))
        sha = hashlib.sha256(canonical_json(blob).encode()).hexdigest()
        entries.append({"name": name, "size": size, "sha256": sha, "file": rel})
    manifest = {"type": "mcs-protein", "version": 1, "proteins": entries}
    (tmp_path / "manifest.json").write_text(canonical_json(manifest))
    return "file://" + str(tmp_path / "manifest.json")


_DEMO_PROTEINS = [
    ("P1", 6, [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [0, 3]]),
    ("P2", 6, [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [1, 4]]),
    ("P3", 5, [[0, 1], [1, 2], [2, 3], [3, 4], [0, 2]]),
]


def test_manifest_corpus_loads_and_verifies(tmp_path):
    from crick.corpus import ManifestCorpus, manifest_corpus_from_url
    url = _write_corpus(tmp_path, _DEMO_PROTEINS)
    corpus = manifest_corpus_from_url(url)
    assert corpus.problem_type == "mcs-protein"
    assert corpus.size == 3 * 3
    prob = corpus.instance(1)                       # pair (1, 0)
    sol = prob.improve(None, attempts=200)
    assert sol and prob.verify(sol)
    # committing the wrong manifest hash is rejected
    with pytest.raises(ValueError):
        ManifestCorpus(url, "00" * 32)


def test_manifest_corpus_rejects_tampered_blob(tmp_path):
    from crick.corpus import manifest_corpus_from_url
    url = _write_corpus(tmp_path, _DEMO_PROTEINS)
    corpus = manifest_corpus_from_url(url)
    # corrupt a blob on the "mirror" after the manifest hash was pinned
    (tmp_path / "proteins" / "P1.json").write_text('{"name":"P1","size":6,"edges":[]}')
    with pytest.raises(ValueError):
        corpus.instance(0)                          # pair touching P1 -> hash mismatch


def test_chain_on_real_manifest_corpus(tmp_path, wallet):
    from crick.chain import Blockchain
    url = _write_corpus(tmp_path, _DEMO_PROTEINS)
    chain = Blockchain.create(manifest_url=url)
    assert chain.corpus_size == 9
    chain.add_block(mine_block(chain, wallet, solve=True))
    assert chain.blocks[1].has_solution
    assert chain.problem.verify(chain.best_solution)
    # genesis commits only the manifest hash, not the data
    assert "manifest_sha256" in chain.blocks[0].problem
    assert "seed" not in chain.blocks[0].problem
    # replay must re-fetch + hash-verify the corpus and reach the same tip
    restored = Blockchain.from_block_dicts([b.to_dict() for b in chain.blocks])
    assert restored.tip.hash == chain.tip.hash

