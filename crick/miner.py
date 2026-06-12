"""Mining: alternate between improving the puzzle solution and grinding nonces.

Per the protocol, a miner that finds a strictly better solution earns the
right to mine at the reduced difficulty d_r; otherwise it mines classically
at d_b. The loop interleaves short solver bursts with nonce chunks so a
single thread does both, and re-checks the chain tip between chunks so a
networked miner abandons stale work.
"""

import random
from typing import Callable, List, Optional

from . import params
from .block import Block, Transaction, hash_with_nonce, now
from .chain import Blockchain, target_for
from .crypto import Wallet

NONCE_CHUNK = 20_000
SOLVER_ATTEMPTS = 50


def build_candidate(chain: Blockchain, wallet: Wallet,
                    mempool: Optional[List[Transaction]] = None,
                    solve: bool = True) -> Block:
    solution = None
    if solve:
        solution = chain.problem.improve(chain.best_solution, attempts=SOLVER_ATTEMPTS)
    txs = [Transaction.coinbase(wallet.address, params.BLOCK_REWARD, chain.height + 1)]
    txs += list(mempool or [])
    return Block(
        height=chain.height + 1,
        prev_hash=chain.tip.hash,
        timestamp=now(),
        miner=wallet.address,
        difficulty=chain.next_difficulty(with_solution=solution is not None),
        transactions=txs,
        solution=solution,
    )


def mine_block(chain: Blockchain, wallet: Wallet,
               mempool: Optional[List[Transaction]] = None,
               solve: bool = True,
               should_abort: Optional[Callable[[], bool]] = None) -> Optional[Block]:
    """Mine one block on the current tip. Returns None if aborted."""
    block = build_candidate(chain, wallet, mempool, solve)
    base = block.base_string()
    target = target_for(block.difficulty)
    nonce = random.getrandbits(32)
    while True:
        for _ in range(NONCE_CHUNK):
            if int(hash_with_nonce(base, nonce), 16) < target:
                block.nonce = nonce
                return block
            nonce += 1
        if should_abort and should_abort():
            return None
        # refresh the timestamp (and pick up a newly found solution) between chunks
        block = build_candidate(chain, wallet, mempool, solve)
        base = block.base_string()
        target = target_for(block.difficulty)
