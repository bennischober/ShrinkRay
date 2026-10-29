"""Full-movie VMAF verification of existing encodes (the Mandalorian re-run).

This is the evidence script for the question the 2026-10-21 plan could not
answer: does a full encode actually sit at the target VMAF? It measures the
COMPLETE encode against the COMPLETE source (not the 10s probes the CRF
search used) and appends every result to test_media/scratch/verify_results.md
so partial runs lose nothing.

Usage (from anywhere uv exists — PEP 723, issue #179):
    uv run scripts/verify_fullmovie.py            # all pairs
    uv run scripts/verify_fullmovie.py --only mandalorian

Run against an installed ShrinkRay (pipx/uv tool) or inside the checkout —
the header's dependency list is empty because shrinkray itself provides
measure_vmaf. Each pair measures 3-20 minutes (4K HDR: the Mandalorian took
~40 min). Results append incrementally; a Ctrl+C keeps what finished.

Failure evidence: measure_vmaf writes stderr + command + a partial
vmaf.json into logs/ next to metadata.db (the 2026-10-21 failure produced
an EMPTY error message — this is the fix for that blindness).
"""
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import argparse
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shrinkray.telemetry import logs_dir
from shrinkray.tools.vmaf import measure_vmaf
from shrinkray.tools.process import Cancelled

REPO = Path(__file__).resolve().parent.parent
RESULT_FILE = REPO / "test_media" / "scratch" / "verify_results.md"

# (label, encoded file, reference file) — encoded first, source second
PAIRS = [
    (
        "rick_morty_s01e01 (CRF 24)",
        "test_media/out_quality/rick_morty_s01e01.mkv",
        "test_media/movies/rick_morty_s01e01.mkv",
    ),
    (
        "bloodshot_alt_ending (CRF 20)",
        "test_media/out_quality/bloodshot_alt_ending.mkv",
        "test_media/movies/bloodshot_alt_ending.mkv",
    ),
    (
        "django_unchained (CRF 20)",
        "test_media/out_quality/django_unchained.mkv",
        "test_media/movies/django_unchained.mkv",
    ),
    (
        "the_mandalorian_s01e02 (CRF 14, HDR)",
        "test_media/out_hdr/the_mandalorian_s01e02.mkv",
        r"G:\Serien\The Mandalorian\Season 1\The Mandalorian - S01E02 - Chapter 2- The Child.mkv",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="measure only pairs whose label contains this string")
    args = parser.parse_args()

    pairs = [p for p in PAIRS if not args.only or args.only.lower() in p[0].lower()]
    if not pairs:
        print(f"no pair matches --only {args.only!r}")
        return 1

    done: set[str] = set()
    if RESULT_FILE.exists():
        for line in RESULT_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                done.add(line[3:].strip())
        print(f"resuming: {len(done)} pairs already have results")

    failures = 0
    # the measurement child runs in its own session (tools/process.py), so a
    # Ctrl+C/SIGTERM would not reach it: the handler converts the signal into
    # the cancel event, and measure_vmaf terminates the tree (issue #431)
    cancel = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    signal.signal(signal.SIGTERM, lambda *_: cancel.set())
    for label, encoded_rel, reference in pairs:
        if label in done:
            print(f"skip (already measured): {label}")
            continue
        encoded = (REPO / encoded_rel).resolve()
        if not encoded.is_file():
            print(f"MISSING encode: {encoded}")
            failures += 1
            continue
        print(f"measuring {label} ... (this can take many minutes)")
        try:
            score = measure_vmaf(
                encoded,
                Path(reference),
                workdir=REPO / "test_media" / "scratch",
                hdr="HDR" in label,
                db_file=REPO / "metadata.db",
                cancel=cancel,
            )
            with RESULT_FILE.open("a", encoding="utf-8") as fh:
                fh.write(f"## {label}\nFULL-MOVIE VMAF: {score:.2f}\n\n")
            print(f"  => VMAF {score:.2f}  (written to {RESULT_FILE.name})")
        except Cancelled:
            print("cancelled; partial results already written")
            return 130
        except Exception as e:
            failures += 1
            bundle = logs_dir(REPO / "metadata.db")
            with RESULT_FILE.open("a", encoding="utf-8") as fh:
                fh.write(f"## {label}\nFAILED: {type(e).__name__}: {e}\n")
                fh.write(f"evidence: {bundle}\n\n")
            print(f"  => FAILED: {e}\n     evidence in {bundle}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
