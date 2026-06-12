#!/usr/bin/env python3
"""Offline corpus builder: real protein structures -> integer contact graphs.

Downloads PDB structures, extracts each chain's C-alpha contact graph (an edge
when two residues' C-alpha atoms are within 8 A), and writes one content-hashed
JSON blob per protein plus a manifest. The float distance math happens HERE,
once, offline; the committed output is pure integers, so consensus nodes only
ever read the integer edges and verify the blob's sha256 — never recompute
distances. This is what makes real data deterministic across nodes.

Usage:
    python tools/build_corpus.py --out /path/to/corpus [--max-res 48] [PDBID ...]

Produces:
    <out>/proteins/<id>.json      one blob per protein (canonical JSON)
    <out>/manifest.json           {type, version, proteins:[{name,size,sha256,file}]}
and prints the manifest's sha256 (commit this in genesis).
"""

import argparse
import hashlib
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crick.crypto import canonical_json  # noqa: E402

# Small proteins / domains, kept compact so the maximum-common-subgraph search
# (max-clique on the modular product) stays tractable in the reference solver.
DEFAULT_PDB_IDS = ["1CRN", "1L2Y", "1VII", "1ENH", "1PGB", "5PTI",
                   "1BDD", "2F4K", "1FME", "1CTF", "1UBQ", "1WLA"]

CONTACT_ANGSTROM = 8.0


def blob_sha256(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def fetch_pdb(pdb_id: str) -> str:
    url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("latin-1")


def contact_graph(pdb_text: str, max_res: int):
    """C-alpha contact graph of the first chain of the first model, truncated to
    max_res residues. Handles multi-model (NMR) entries and alternate locations."""
    cas = []
    seen_chain = None
    for line in pdb_text.splitlines():
        if line.startswith("ENDMDL"):
            break  # first model only (NMR structures have many)
        if not line.startswith("ATOM"):
            continue
        if line[12:16].strip() != "CA":
            continue
        altloc = line[16]
        if altloc not in (" ", "A"):
            continue  # skip alternate conformations of the same atom
        chain = line[21]
        if seen_chain is None:
            seen_chain = chain
        if chain != seen_chain:
            break  # first chain only
        x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
        cas.append((x, y, z))
        if len(cas) >= max_res:
            break
    n = len(cas)
    cutoff2 = CONTACT_ANGSTROM ** 2
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = cas[i][0] - cas[j][0]
            dy = cas[i][1] - cas[j][1]
            dz = cas[i][2] - cas[j][2]
            if dx * dx + dy * dy + dz * dz < cutoff2:
                edges.append([i, j])
    return n, edges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output corpus directory")
    ap.add_argument("--max-res", type=int, default=48, help="cap residues per protein")
    ap.add_argument("ids", nargs="*", help="PDB IDs (default: a small curated set)")
    args = ap.parse_args()

    ids = [i.upper() for i in (args.ids or DEFAULT_PDB_IDS)]
    os.makedirs(os.path.join(args.out, "proteins"), exist_ok=True)

    proteins = []
    for pid in ids:
        try:
            n, edges = contact_graph(fetch_pdb(pid), args.max_res)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {pid}: {e}", file=sys.stderr)
            continue
        if n < 8:
            print(f"  skip {pid}: only {n} residues", file=sys.stderr)
            continue
        blob = {"name": pid, "size": n, "edges": edges}
        rel = f"proteins/{pid}.json"
        with open(os.path.join(args.out, rel), "w") as f:
            f.write(canonical_json(blob))
        proteins.append({"name": pid, "size": n, "sha256": blob_sha256(blob), "file": rel})
        print(f"  {pid}: {n} residues, {len(edges)} contacts")

    if len(proteins) < 2:
        sys.exit("need at least 2 proteins to form MCS pairs")

    manifest = {"type": "mcs-protein", "version": 1, "proteins": proteins}
    manifest_text = canonical_json(manifest)
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        f.write(manifest_text)
    h = hashlib.sha256(manifest_text.encode()).hexdigest()
    print(f"\n{len(proteins)} proteins -> {len(proteins) ** 2} MCS instances")
    print(f"manifest.json sha256 = {h}")
    print("commit this hash in genesis (crick init --manifest <url> pins it automatically)")


if __name__ == "__main__":
    main()
