"""Measure one reference/distorted pair with both ShrinkRay and ab-av1 (issue #306).

The manual leg of the VMAF parity check: `tests/test_vmaf_parity.py` proves the
recipe on synthetic clips generated at test time, this script runs the SAME
comparison on real content from `test_media/` - grain, letterboxing, HDR, an
open-GOP source, the things a lavfi clip cannot show. The procedure it serves is
docs/vmaf-parity.md.

The comparison is the MEASUREMENT, not the search: ab-av1 encodes with ffmpeg's
libsvtav1, ShrinkRay with HandBrakeCLI, so comparing chosen CRFs would differ
for reasons unrelated to VMAF.

Usage (from the repository root, with the pinned tools on PATH):

    uv run scripts/vmaf_parity.py --pair ref.mkv dis.mkv
    uv run scripts/vmaf_parity.py --pair ref.mkv dis.mkv --label "10-bit"
    uv run scripts/vmaf_parity.py --pair ref.mkv dis.mkv --crop 140,140,0,0
    uv run scripts/vmaf_parity.py --pair ref.mkv dis.mkv --hdr

Each pair measures ShrinkRay's score and ab-av1's score on the same two files and
prints the delta. Before scoring, both sides' video frame counts are compared: a
pair whose counts differ is not comparing the same frames, so it is reported as
unmeasured and fails the run instead of printing a meaningless number (issue #408).
The tolerance is |delta| <= 0.1 VMAF (issue #306); an HDR pair is
measured TWICE - with tone-mapping on (the shipped ShrinkRay path, REPORTED against
ab-av1's PQ-direct score as the documented deviation in docs/encode-decision.md)
and with tone-mapping off (the like-for-like comparison, HELD). Results append to
test_media/scratch/vmaf_parity.md, so a long run keeps what finished.

ab-av1 is a development tool only: nothing under src/ invokes it, and it is no
dependency. It is invoked here with --vmaf-fps 0 so both sides score the source's own
timestamps (ab-av1's default would re-time both inputs to 25 fps).
"""

# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shrinkray.tools.vmaf import measure_vmaf, vmaf_recipe

REPO = Path(__file__).resolve().parent.parent
RESULT_FILE = REPO / "test_media" / "scratch" / "vmaf_parity.md"

# |ShrinkRay - ab-av1| per pair (issue #306's proposed tolerance)
TOLERANCE = 0.1

AB_AV1 = "ab-av1"


def parse_crop(value: str | None) -> tuple[int, int, int, int] | None:
    """Parses a `top,bottom,left,right` crop tuple.

    Args:
        value (str | None): The raw argument, or None.

    Returns:
        tuple[int, int, int, int] | None: The crop, or None when absent.

    Raises:
        argparse.ArgumentTypeError: If the value is not four integers.
    """
    if not value:
        return None
    try:
        parts = [int(v) for v in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("--crop takes top,bottom,left,right as integers") from None
    if len(parts) != 4 or any(v < 0 for v in parts):
        raise argparse.ArgumentTypeError("--crop takes four non-negative integers")
    return parts[0], parts[1], parts[2], parts[3]


def ab_av1_vmaf(reference: Path, distorted: Path, crop: tuple[int, int, int, int] | None) -> float:
    """Measures the pair with `ab-av1 vmaf`, the reference implementation.

    Args:
        reference (Path): The pristine source.
        distorted (Path): The encoded file.
        crop (tuple | None): Crop bars applied to the reference, as
            `--reference-vfilter crop=...` (ab-av1's mechanism for exactly this).

    Returns:
        float: ab-av1's reported score.

    Raises:
        RuntimeError: If ab-av1 is missing, fails, or prints no score.
    """
    binary = shutil.which(AB_AV1)
    if binary is None:
        raise RuntimeError(f"{AB_AV1} not found on PATH (see docs/vmaf-parity.md)")
    command = [
        binary,
        "vmaf",
        "--reference",
        str(reference),
        "--distorted",
        str(distorted),
        # ab-av1 defaults to -r 25 on both inputs; 0 disables it so both sides
        # score the source's own timestamps (ShrinkRay does)
        "--vmaf-fps",
        "0",
    ]
    if crop is not None:
        top, bottom, left, right = crop
        # x = left, y = top (the crop tuple is top,bottom,left,right)
        command += [
            "--reference-vfilter",
            f"crop=iw-{left + right}:ih-{top + bottom}:{left}:{top}",
        ]
    print("AB-AV1: " + " ".join(command))
    proc = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"ab-av1 vmaf failed: {proc.stderr[-400:]}")
    lines = proc.stdout.strip().splitlines()
    if not lines:
        raise RuntimeError("ab-av1 vmaf printed no score")
    return float(lines[-1])


def video_frame_count(path: Path) -> int | None:
    """Counts a file's video frames, the check that catches a misaligned pair.

    A pair whose sides have different frame counts is not comparing the same
    frames, so its score is meaningless however closely the two tools agree
    (issue #408). Counting decodes the file, which is why it belongs to this
    harness and not to every measurement the product runs.

    Args:
        path (Path): The media file.

    Returns:
        int | None: The video frame count, or None when ffprobe could not report
            one (missing binary, unreadable file, no video stream).
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def measure_ours(
    reference: Path,
    distorted: Path,
    crop: tuple[int, int, int, int] | None,
    hdr: bool,
) -> tuple[float, str]:
    """Measures the pair with ShrinkRay's own measurement boundary.

    Args:
        reference (Path): The pristine source.
        distorted (Path): The encoded file.
        crop (tuple | None): Crop bars applied to the reference.
        hdr (bool): Tone-map both inputs to SDR before scoring.

    Returns:
        tuple: The score and the recipe identifier it was measured with (the
            recipe is what the caches key on, so recording it keeps the result
            comparable with a later run).
    """
    recipe = vmaf_recipe(crop=crop, hdr=hdr)
    with tempfile.TemporaryDirectory(prefix="shrinkray_parity_") as tmp:
        score = measure_vmaf(
            distorted,
            reference,
            Path(tmp),
            crop=crop,
            hdr=hdr,
            recipe=recipe,
        )
    return score, recipe.identifier()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pair",
        nargs=2,
        action="append",
        metavar=("REFERENCE", "DISTORTED"),
        required=True,
        help="a reference and a distorted file (repeatable)",
    )
    parser.add_argument("--label", action="append", default=[], help="label per --pair, in order")
    parser.add_argument(
        "--crop",
        action="append",
        default=[],
        type=parse_crop,
        help="top,bottom,left,right per --pair, in order (omit to keep none)",
    )
    parser.add_argument(
        "--hdr",
        action="append",
        default=[],
        type=lambda v: v.lower() in ("1", "true", "yes", "y"),
        help="true/false per --pair, in order (default false)",
    )
    args = parser.parse_args()

    pairs = [(Path(ref), Path(dis)) for ref, dis in args.pair]
    labels = list(args.label) + [f"pair {i + 1}" for i in range(len(args.pair))]
    crops = list(args.crop) + [None] * len(args.pair)
    hdrs = list(args.hdr) + [False] * len(args.pair)

    for reference, distorted in pairs:
        for path in (reference, distorted):
            if not path.is_file():
                print(f"MISSING: {path}")
                return 1

    rows: list[tuple[str, float | None, float | None, str, str]] = []
    failures = 0
    for index, (reference, distorted) in enumerate(pairs):
        label = labels[index]
        crop = crops[index]
        hdr = hdrs[index]
        print(f"\n=== {label} ===")
        reference_frames = video_frame_count(reference)
        distorted_frames = video_frame_count(distorted)
        print(f"frames: reference {reference_frames}, distorted {distorted_frames}")
        if reference_frames is None or distorted_frames is None:
            print(
                f"{label}: UNMEASURABLE - the frame counts could not be read, "
                "so frame alignment cannot be established"
            )
            failures += 1
            rows.append(
                (f"{label} [frame counts unreadable]", None, None, "unmeasured", "")
            )
            continue
        if reference_frames != distorted_frames:
            # A count mismatch means the two sides are not comparing the same
            # frames: the score would be meaningless even where both tools agree
            # to four decimals (issue #408 measured 17.71 on a 1448/1445 pair).
            print(
                f"{label}: MISALIGNED - {reference_frames} vs {distorted_frames} frames "
                f"(delta {reference_frames - distorted_frames:+d}); not scoring this pair"
            )
            failures += 1
            rows.append(
                (
                    f"{label} [misaligned: {reference_frames} vs {distorted_frames} frames]",
                    None,
                    None,
                    "unmeasured",
                    "",
                )
            )
            continue
        ours, recipe = measure_ours(reference, distorted, crop, hdr)
        theirs = ab_av1_vmaf(reference, distorted, crop)
        delta = abs(ours - theirs)
        print(f"{label}: ShrinkRay {ours:.4f} vs ab-av1 {theirs:.4f} (delta {delta:.4f})")
        print(f"  recipe: {recipe}")
        if hdr:
            # the tone-map is #310's documented deviation: REPORTED, not held
            rows.append((f"{label} [tone-mapped, reported]", ours, theirs, "reported", recipe))
            # the like-for-like comparison: both sides score the PQ signal directly
            ours_pq, recipe_pq = measure_ours(reference, distorted, crop, False)
            delta_pq = abs(ours_pq - theirs)
            print(
                f"{label} [PQ-direct]: ShrinkRay {ours_pq:.4f} vs ab-av1 {theirs:.4f} "
                f"(delta {delta_pq:.4f})"
            )
            rows.append((f"{label} [PQ-direct]", ours_pq, theirs, "", recipe_pq))
            if delta_pq > TOLERANCE:
                failures += 1
            continue
        rows.append((label, ours, theirs, "", recipe))
        if delta > TOLERANCE:
            failures += 1

    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RESULT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"## {', '.join(labels)}\n\n")
        fh.write("| pair | ShrinkRay | ab-av1 | delta | verdict | recipe |\n")
        fh.write("|---|---|---|---|---|---|\n")
        for label, ours, theirs, note, recipe in rows:
            if note == "unmeasured" or ours is None or theirs is None:
                fh.write(f"| {label} | — | — | — | unmeasured | — |\n")
                continue
            delta = abs(ours - theirs)
            verdict = note or ("PASS" if delta <= TOLERANCE else "FAIL")
            fh.write(
                f"| {label} | {ours:.4f} | {theirs:.4f} | {delta:.4f} | {verdict} | `{recipe}` |\n"
            )
        fh.write("\n")

    print(f"\nresults appended to {RESULT_FILE}")
    unmeasured = sum(1 for row in rows if row[3] == "unmeasured")
    if failures:
        # an unmeasured pair was never compared, so it is not "outside the
        # tolerance": the two outcomes are counted apart
        print(
            f"{failures} pair(s) did not produce a usable comparison "
            f"({unmeasured} unmeasured, {failures - unmeasured} outside the {TOLERANCE} tolerance)"
        )
        return 1
    print(f"every held comparison is within {TOLERANCE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
