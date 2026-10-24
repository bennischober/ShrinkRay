"""The output-encoder choice: one function, one codec family.

PRODUCT.md makes AV1 the only output codec; this module owns the
choice so no call site re-derives it (issue #21, #26).
"""

from __future__ import annotations

from shrinkray.engine.search import DEFAULT_TARGET_VMAF

# AV1 is the only output codec (issue #21): SVT-AV1 on the CPU, 10-bit for
# HDR. x265 is gone from every path - not demoted to a fallback, not kept
# as an option. encoder_for() is the single source of this choice.
DEFAULT_ENCODER = "svt_av1"
HDR_ENCODER = "svt_av1_10bit"
# HDR size estimates carry a measured, systematic bias (issue #98): the size
# probe encodes ONE early window, and HDR samples skew toward easy content
# (dark, static), so the predicted ratio lands far below the real one —
# measured on an HDR 4K title: 19.1% real savings vs 78% predicted. The
# estimate is still shown (it is the only prediction the pipeline has) but
# every surface that shows it carries this note; the README documents the
# same numbers. Quoted, never recomputed: the factor itself is a
# measurement this codebase cannot remake without the original title.
HDR_ESTIMATE_NOTE = (
    "HDR estimate: measured on one easy-content window; real savings run "
    "much lower (measured 19.1% real vs 78% predicted on an HDR 4K title)"
)
# HDR measurement runs on tone-mapped SDR twins, whose scale sits slightly
# below the SDR-native one (a Mandalorian sample: CRF 14 = 94.78, CRF 10 =
# 96.49). The calibrated offset is -0.5, expressed here as a difference so
# the two defaults cannot drift apart again (issue #103).
HDR_VMAF_OFFSET = 0.5
DEFAULT_HDR_TARGET_VMAF = DEFAULT_TARGET_VMAF - HDR_VMAF_OFFSET


def encoder_for(hdr: bool) -> str:
    """Returns the only output encoder this product emits.

    PRODUCT.md makes AV1 the single output codec, encoded on the CPU: SDR
    encodes with ``svt_av1`` and HDR encodes with ``svt_av1_10bit``. H.264
    and H.265 are not demoted to fallbacks - they are gone. Keeping the
    choice in one function is what stops the six HDR ternaries that used to
    be written out per call site from drifting apart again.

    Args:
        hdr (bool): True when the source is HDR.

    Returns:
        str: The HandBrake encoder name.
    """
    return HDR_ENCODER if hdr else DEFAULT_ENCODER
