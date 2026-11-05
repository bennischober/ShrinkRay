"""The output-encoder choice: one function, one codec family.

PRODUCT.md makes AV1 the only output codec; this module owns the
choice so no call site re-derives it (issue #21, #26).
"""

from __future__ import annotations

# AV1 is the only output codec (issue #21): SVT-AV1 on the CPU, 10-bit for
# 10-bit sources. x265 is gone from every path - not demoted to a fallback,
# not kept as an option. encoder_for() is the single source of this choice.
# HDR sources never reach it: the skip gate (issue #518) refuses them first.
DEFAULT_ENCODER = "svt_av1"
HDR_ENCODER = "svt_av1_10bit"
# HDR size estimates carry a measured, systematic bias (issue #98): the size
# estimate comes from the probe windows (all of them when the probe cache
# carries their encoded sizes, one window otherwise — issue #388), and HDR
# samples skew toward easy content (dark, static), so the predicted ratio
# lands far below the real one — measured on an HDR 4K title: 19.1% real
# savings vs 78% predicted. HDR sources are SKIPPED since issue #518 (the
# maintainer's #340 decision), so no new estimate is ever made — the note
# annotates the HISTORICAL plan rows (hdr=1 in the plans table) whose
# estimates still render. Quoted, never recomputed: the factor itself is a
# measurement this codebase cannot remake without the original title.
HDR_ESTIMATE_NOTE = (
    "HDR estimate: the prediction comes from easy-content probe windows "
    "(all of them when the probe cache carries their encoded sizes, one "
    "window otherwise); real savings run much lower (measured 19.1% real vs "
    "78% predicted on an HDR 4K title)"
)


def encoder_for(bit_depth: int | None) -> str:
    """Returns the only output encoder this product emits.

    PRODUCT.md makes AV1 the single output codec, encoded on the CPU. The
    choice follows the SOURCE's bit depth (issue #234): any 10-bit source
    stays 10-bit (``svt_av1_10bit``), an 8-bit source keeps ``svt_av1`` —
    10-bit is not an HDR property, and dropping the two extra bits of a
    10-bit SDR master is a quality loss the product's own rules forbid. An
    unknown depth keeps the default encoder - that is what the code always
    did (a behaviour change needs its own issue). HDR sources never reach
    this choice: the skip gate (issue #518) refuses them before any
    measurement.

    Args:
        bit_depth (int | None): The source's bit depth (None: unknown).

    Returns:
        str: The HandBrake encoder name.
    """
    # an UNKNOWN depth keeps the 8-bit encoder: that is what the code always
    # did (the old docstring's "falls back to the HDR answer" was wrong - the
    # tests pinned DEFAULT for None); a behaviour change needs its own issue
    return HDR_ENCODER if bit_depth is not None and bit_depth > 8 else DEFAULT_ENCODER
