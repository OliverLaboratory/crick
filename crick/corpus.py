"""Corpora: where epoch instances come from.

A corpus exposes `.size` (how many instances chain entropy selects among) and
`.instance(index) -> Problem`. Two kinds:

  SyntheticCorpus  — instances generated deterministically from a seed string
                     (the self-contained prototype / test mode).

  ManifestCorpus   — instances assembled from REAL data blobs that are
                     content-addressed: the genesis block commits a manifest
                     hash, the manifest lists each blob's sha256, and every
                     fetched blob is checked against it. The data can therefore
                     be served from an untrusted mirror (HTTP, IPFS, storage
                     nodes) without weakening consensus — a wrong byte changes
                     the hash and is rejected. Determinism then rests only on
                     the integer verifier/scorer, not on any shared RNG.

`corpus_from_genesis(spec)` builds the right one from the genesis problem spec.
"""

import hashlib
import json
import urllib.request

from .crypto import canonical_json
from .puzzle import new_problem
from .bioproblems import MCSProblem


def blob_sha256(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def fetch_bytes(url: str, timeout: float = 30.0) -> bytes:
    """Fetch http(s):// or file:// URLs. Used to pull the manifest and blobs."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


# ----------------------------------------------------------------- synthetic

class SyntheticCorpus:
    def __init__(self, problem_type: str, seed: str, size: int):
        self.problem_type = problem_type
        self.seed = seed
        self.size = size

    def instance(self, index: int):
        return new_problem(self.problem_type, f"{self.seed}:i:{index}")

    def genesis_spec(self) -> dict:
        return {"type": self.problem_type, "seed": self.seed, "corpus_size": self.size}


# ------------------------------------------------------------------ manifest

class ManifestCorpus:
    """Real, content-addressed instances. Currently mcs-protein: instance
    `index` is the pair (i, j) of proteins, i = index % N, j = (index//N) % N."""

    def __init__(self, manifest_url: str, manifest_sha256: str, fetch=fetch_bytes):
        self._fetch = fetch
        self.manifest_url = manifest_url
        self.manifest_sha256 = manifest_sha256
        raw = self._fetch(manifest_url)
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            raise ValueError("manifest hash does not match the value committed in genesis")
        self.manifest = json.loads(raw.decode())
        self.problem_type = self.manifest["type"]
        if self.problem_type != "mcs-protein":
            raise ValueError(f"manifest corpus not supported for {self.problem_type!r} yet")
        self.entries = self.manifest["proteins"]
        self._base = manifest_url.rsplit("/", 1)[0] + "/"
        self._cache: dict = {}
        n = len(self.entries)
        if n < 2:
            raise ValueError("corpus needs >= 2 proteins")
        self.size = n * n  # pair space

    def _blob(self, k: int) -> dict:
        if k not in self._cache:
            entry = self.entries[k]
            raw = self._fetch(self._base + entry["file"])
            obj = json.loads(raw.decode())
            if blob_sha256(obj) != entry["sha256"]:
                raise ValueError(f"blob {entry['name']} hash mismatch (untrusted/corrupt mirror)")
            self._cache[k] = obj
        return self._cache[k]

    def instance(self, index: int):
        n = len(self.entries)
        i = index % n
        j = (index // n) % n
        if i == j:
            j = (j + 1) % n
        return MCSProblem.from_spec({"type": "mcs-protein",
                                     "a": self._blob(i), "b": self._blob(j)})

    def genesis_spec(self) -> dict:
        return {"type": self.problem_type, "manifest_url": self.manifest_url,
                "manifest_sha256": self.manifest_sha256}


# -------------------------------------------------------------------- factory

def corpus_from_genesis(spec: dict, fetch=fetch_bytes):
    if "manifest_url" in spec:
        return ManifestCorpus(spec["manifest_url"], spec["manifest_sha256"], fetch=fetch)
    return SyntheticCorpus(spec["type"], spec["seed"], int(spec.get("corpus_size", 1_000_000)))


def manifest_corpus_from_url(manifest_url: str, fetch=fetch_bytes) -> "ManifestCorpus":
    """Build a corpus from a live manifest URL, pinning its current hash
    (used by `crick init --manifest`)."""
    raw = fetch(manifest_url)
    return ManifestCorpus(manifest_url, hashlib.sha256(raw).hexdigest(), fetch=fetch)
