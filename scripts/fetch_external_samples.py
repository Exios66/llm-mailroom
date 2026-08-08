#!/usr/bin/env python3
"""Fetch the external pilot samples from LegalBench, The Atticus Project, and
Pile of Law.

The pilot set (examples/samples/manifest.csv) gains 18 real legal documents —
6 from each source. This script downloads them once into the repo so they are
committed and every later step (prepare_samples, run_pilot, sync_dataset) works
offline:

- legalbench  -> examples/external/legalbench/<id>.txt      (6 MAUD merger
                 agreements — the full contract texts behind LegalBench's 34
                 `maud_*` tasks; MAUD v1, Zenodo 10.5281/zenodo.7500064,
                 CC BY 4.0)
- atticus     -> examples/samples/contract/<id>_<type>.pdf  (6 real CUAD SEC-
                 exhibit contract PDFs; theatticusproject/cuad, CC BY 4.0)
- pileoflaw   -> examples/external/pileoflaw/<id>.txt       (6 U.S. court
                 opinions from the `courtlistener_opinions` subset — U.S.
                 government works, public domain; Pile of Law compilation is
                 CC BY-NC-SA 4.0 but these individual works are PD, so only
                 public-domain subsets are used)

Idempotent: existing files are kept (--force to re-download).

Usage:
    python scripts/fetch_external_samples.py            # all three sources
    python scripts/fetch_external_samples.py --source legalbench
    python scripts/fetch_external_samples.py --force
"""

from __future__ import annotations

import argparse
import json
import lzma
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTERNAL_DIR = REPO_ROOT / "examples" / "external"
CUAD_CONTRACT_DIR = REPO_ROOT / "examples" / "samples" / "contract"

_UA = {"User-Agent": "mailroom-pilot-sampler/1.0 (research sampling)"}

# ---------------------------------------------------------------------------
# LegalBench — MAUD v1 merger agreements (the full texts behind the maud_* tasks)
# ---------------------------------------------------------------------------

MAUD_ZIP_URL = "https://zenodo.org/records/7500064/files/maud_v1.zip?download=1"
MAUD_ZIP_PATH = Path(__import__("tempfile").gettempdir()) / "maud_v1.zip"

# (sample id, zip member, size tier) — picks span the size range
# (106KB … 792KB of text, i.e. ~15 … ~100 rendered pages).
MAUD_PICKS = [
    ("legalbench_01", "data/contracts/contract_88.txt", "small"),
    ("legalbench_02", "data/contracts/contract_83.txt", "medium"),
    ("legalbench_03", "data/contracts/contract_129.txt", "medium"),
    ("legalbench_04", "data/contracts/contract_71.txt", "large"),
    ("legalbench_05", "data/contracts/contract_117.txt", "large"),
    ("legalbench_06", "data/contracts/contract_68.txt", "large"),
]

# ---------------------------------------------------------------------------
# The Atticus Project — CUAD v1 contract PDFs (SEC filing exhibits)
# ---------------------------------------------------------------------------

HF_RAW = "https://huggingface.co/datasets/theatticusproject/cuad/resolve/main/"
# (sample id, HF path, agreement type for the committed filename)
CUAD_PICKS = [
    (
        "atticus_01",
        "CUAD_v1/full_contract_pdf/Part_II/Commercial Contracts (Part II-A)/IP/"
        "INGEVITYCORP_05_16_2016-EX-10.5-INTELLECTUAL PROPERTY AGREEMENT.PDF",
        "ip_agreement",
    ),
    (
        "atticus_02",
        "CUAD_v1/full_contract_pdf/Part_I/License_Agreements/"
        "ArtaraTherapeuticsInc_20200110_8-K_EX-10.5_11943350_EX-10.5_License Agreement.pdf",
        "license_agreement",
    ),
    (
        "atticus_03",
        "CUAD_v1/full_contract_pdf/Part_I/Supply/"
        "LohaCompanyltd_20191209_F-1_EX-10.16_11917878_EX-10.16_Supply Agreement.pdf",
        "supply_agreement",
    ),
    (
        "atticus_04",
        "CUAD_v1/full_contract_pdf/Part_II/Commercial Contracts (Part II-A)/Franchise/"
        "BUFFALOWILDWINGSINC_06_05_1998-EX-10.3-FRANCHISE AGREEMENT.PDF",
        "franchise_agreement",
    ),
    (
        "atticus_05",
        "CUAD_v1/full_contract_pdf/Part_II/Commercial Contracts (Part II-A)/Distributor/"
        "NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBUTOR AGREEMENT.pdf",
        "distributor_agreement",
    ),
    (
        "atticus_06",
        "CUAD_v1/full_contract_pdf/Part_I/Joint Venture/"
        "ACCELERATEDTECHNOLOGIESHOLDINGCORP_04_24_2003-EX-10.13-JOINT VENTURE AGREEMENT.PDF",
        "joint_venture_agreement",
    ),
]

# ---------------------------------------------------------------------------
# Pile of Law — courtlistener_opinions subset (public-domain U.S. court opinions)
# ---------------------------------------------------------------------------

# Only public-domain subsets are sampled (U.S. government works); the Pile of
# Law *compilation* itself is CC BY-NC-SA 4.0 and is not committed.
POL_FILES = [
    "https://huggingface.co/datasets/pile-of-law/pile-of-law/resolve/main/data/"
    "train.courtlisteneropinions.{}.jsonl.xz".format(i)
    for i in range(4)
]
POL_MIN_CHARS = 4000  # skip docket-order stubs
POL_PICKS = 6


def _download(url: str, dest: Path, chunk: int = 1 << 20) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh, chunk)
    return dest


def fetch_legalbench(force: bool) -> list[dict]:
    if not MAUD_ZIP_PATH.exists() or force:
        print(f"  downloading MAUD v1 zip ({MAUD_ZIP_URL})")
        _download(MAUD_ZIP_URL, MAUD_ZIP_PATH)
    written = []
    with zipfile.ZipFile(MAUD_ZIP_PATH) as z:
        for sample_id, member, _tier in MAUD_PICKS:
            dest = EXTERNAL_DIR / "legalbench" / f"{sample_id}_merger_agreement.txt"
            if dest.exists() and not force:
                written.append(dest)
                continue
            dest.write_bytes(z.read(member))
            print(f"  {sample_id} <- {member} ({len(dest.read_bytes()) // 1000} KB)")
            written.append(dest)
    return written


def fetch_atticus(force: bool) -> list[Path]:
    written = []
    for sample_id, hf_path, atype in CUAD_PICKS:
        dest = CUAD_CONTRACT_DIR / f"{sample_id}_{atype}.pdf"
        if dest.exists() and not force:
            written.append(dest)
            continue
        url = HF_RAW + urllib.parse.quote(hf_path)
        print(f"  {sample_id} <- {hf_path.split('/')[-1]}")
        _download(url, dest)
        written.append(dest)
    return written


def _stream_pol_records():
    """Yield (record, url) for courtlistener opinions, streaming + aborting
    early per file (never downloads a whole multi-GB shard)."""
    for url in POL_FILES:
        req = urllib.request.Request(url, headers=_UA)
        decomp = lzma.LZMADecompressor()
        buf = b""
        with urllib.request.urlopen(req, timeout=180) as resp:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                buf += decomp.decompress(chunk)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        yield json.loads(line)
        yield None  # end of this shard


def _caption_from_text(text: str) -> str:
    """Best-effort case caption from the opinion text itself (the CourtListener
    v3 API now requires authentication, so we derive a short label from the
    first meaningful line instead — e.g. 'Court of Appeals of New York')."""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not first:
        return "unknown"
    first = first.replace("—", "").strip(": .,")
    return first[:80]


def fetch_pileoflaw(force: bool) -> list[dict]:
    dest_dir = EXTERNAL_DIR / "pileoflaw"
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in dest_dir.glob("pileoflaw_*.txt")}
    if len(existing) >= POL_PICKS and not force:
        return list(dest_dir.glob("pileoflaw_*.txt"))

    print("  streaming courtlistener_opinions shards (aborting after 6 picks)")
    picked: list[dict] = []
    for rec in _stream_pol_records():
        if rec is None:
            continue
        text = rec.get("text") or ""
        if len(text) < POL_MIN_CHARS:
            continue
        url = rec.get("url") or ""
        caption = _caption_from_text(text)
        n = len(picked) + 1
        sample_id = f"pileoflaw_{n:02d}"
        dest = dest_dir / f"{sample_id}_court_opinion.txt"
        if dest.exists() and not force:
            picked.append({"id": sample_id, "file": dest, "caption": caption})
            continue
        header = f"Case: {caption}\nSource: {url}\n\n"
        dest.write_text(header + text)
        print(f"  {sample_id} <- {caption} ({len(text)} chars)")
        picked.append({"id": sample_id, "file": dest, "caption": caption})
        if len(picked) >= POL_PICKS:
            break
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=["legalbench", "atticus", "pileoflaw"],
        help="Fetch only one source (default: all).",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if files exist.")
    args = parser.parse_args()

    sources = [args.source] if args.source else ["legalbench", "atticus", "pileoflaw"]
    for src in sources:
        print(f"[{src}]")
        if src == "legalbench":
            fetch_legalbench(args.force)
        elif src == "atticus":
            fetch_atticus(args.force)
        else:
            fetch_pileoflaw(args.force)
    print("Done. Files are committed under examples/external/ and examples/samples/contract/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
