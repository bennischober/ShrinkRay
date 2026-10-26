# this module loads the config.json file and provides the settings to the other modules => provides default values
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.types import StrictBool

from shrinkray.logger import log, log_error, log_warning

# fallback values if the config file is missing or a value is not set
DEFAULT_EXTENSIONS = [".mkv"]
DEFAULT_SEPARATOR = ";"
DEFAULT_BLACK_BAR_MIN_WIDTH = 100
DEFAULT_BLACK_BAR_MIN_HEIGHT = 100
DEFAULT_LANGUAGE = "en"
DEFAULT_MAX_THREADS = 0  # 0 = use all cores
DEFAULT_IDLE_PRIORITY = True
# quality mode's encoder speed preset (x264/x265 ladder; see #21 for AV1)
# quality mode's encoder speed presets (x264/x265 ladder; see #21 for AV1).
# TWO presets since #22: the PROBE preset is a measurement setting (the CRF
# search's throwaway encodes), the FINAL preset is an output setting (the kept
# artifact). They are settable - and defaulted - separately on purpose.
DEFAULT_PROBE_ENCODER_PRESET = "5"
DEFAULT_ENCODER_PRESET = "3"
# AV1 is the only output codec (issue #21): SVT-AV1 on the CPU, 10-bit for
# HDR. A config that still names x265 is rejected at load rather than
# silently producing H.265.
OUTPUT_ENCODERS = ("svt_av1", "svt_av1_10bit")
# the speed-preset vocabulary of the x264/x265 encoders (HandBrake passes the
# value through to --encoder-preset; anything else is rejected at load, never
# misparsed by HandBrake)
ENCODER_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
)
# per-encoder preset vocabularies and defaults (issue #22: the issue's numeric
# defaults 5/3 are SVT-AV1 vocabulary; x265 takes names). The SVT-AV1 entry is
# the shape #21 will activate; no encoder selects it yet.
ENCODER_PRESET_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "x264": ENCODER_PRESETS,
    "x265": ENCODER_PRESETS,
    "x265_10bit": ENCODER_PRESETS,
    "svt_av1": tuple(str(n) for n in range(-1, 11)),
}
ENCODER_PRESET_DEFAULTS: dict[str, tuple[str, str]] = {
    # encoder => (probe preset, final preset)
    "x264": (DEFAULT_PROBE_ENCODER_PRESET, DEFAULT_ENCODER_PRESET),
    "x265": (DEFAULT_PROBE_ENCODER_PRESET, DEFAULT_ENCODER_PRESET),
    "x265_10bit": (DEFAULT_PROBE_ENCODER_PRESET, DEFAULT_ENCODER_PRESET),
    "svt_av1": ("5", "3"),
}
# stall watchdog: minutes of child silence before termination (0 = off)
DEFAULT_STALL_MINUTES = 10.0
# eligibility: sources below this height are never re-encoded. PRODUCT.md
# makes 1080p the floor: a lower-resolution source has too few pixels to save
# meaningfully, so the probe cost is spent for nothing. The setting may raise
# the floor (e.g. 2160 = 4K only), never lower it (issue #246)
DEFAULT_MIN_SOURCE_HEIGHT = 1080
# eligibility: the width arm of the same 1080p-class rule (issue #233). A
# 2.39:1 scope film is a 1080p-class master at 1920x800, and a height-only
# floor skipped exactly the widescreen sources this product exists for. One
# class floor, two dimensions: either at class size qualifies.
DEFAULT_MIN_SOURCE_WIDTH = 1920
# cropdetect windows per movie: more samples can only shrink the crop once
# the bars are the ones common to every sample, which is the safe direction
# for an irreversible crop (issue #17)
DEFAULT_CROP_SAMPLE_COUNT = 10
# probe density as a duration: one window per N seconds of runtime, so
# coverage is a fixed fraction of the file instead of shrinking with length
DEFAULT_SAMPLE_EVERY = 600.0
DEFAULT_SAMPLE_COUNT = 3
DEFAULT_SAMPLE_SECONDS = 10.0
# the required VMAF (quality.target_vmaf). 96 is the decided product default
# (Netflix's 'visually transparent' zone; 93 is their common top-tier floor).
# VMAF falls monotonically with rising CRF, so a higher target means a larger
# file. It lives here, below the engine, so the config can default to it; the
# engine re-exports it, so there is still one home (issue #252)
DEFAULT_TARGET_VMAF = 96.0
# eligibility: sources below this size are never re-encoded (0 = gate off).
# Probe cost is roughly constant per file while the payoff scales with size,
# so the smallest files have the worst cost/benefit ratio
DEFAULT_MIN_SOURCE_SIZE_GB = 0.0
# eligibility: the operator's EXTRA source codecs beyond the product whitelist
# (SOURCE_CODECS in engine/plan.py: h264, hevc, vp9, av1). The whitelist is a
# product rule and cannot be shrunk from config; an entry here makes one more
# codec a candidate (e.g. "mpeg4" for a library the operator accepts re-encoding)
DEFAULT_CANDIDATE_CODECS: list[str] = []


class HandbrakeSection(BaseModel):
    """The handbrake section of config.json (the re-encoding settings)."""

    model_config = ConfigDict(extra="forbid")

    preset: str = "Fast 1080p30"
    encoder: str = "svt_av1"
    probe_encoder_preset: str = DEFAULT_PROBE_ENCODER_PRESET
    encoder_preset: str = DEFAULT_ENCODER_PRESET
    max_threads: int = Field(DEFAULT_MAX_THREADS, ge=0)
    stall_minutes: float = Field(DEFAULT_STALL_MINUTES, ge=0)
    # the track filters (issue #240's keys; DEFAULT EMPTY since #297's
    # decision): ISO 639-2 codes. EMPTY = no filter = every track in every
    # language — the DEFAULT, because PRODUCT.md promises 1:1 track copy
    # and invariant 3 makes a track-removing default an un-opted
    # destructive action. The reference preset's lists (audio: deu, eng,
    # und, jpn; subtitle: eng, deu, und) are documented in
    # docs/encode-settings.md as the example for operators who want the
    # narrowing; they are a FILTER an operator opts into, not a default.
    # --audio-lang-list without --all-audio would set the selection to
    # first, so the builder passes the pair together.
    audio_languages: list[str] = []
    subtitle_languages: list[str] = []

    @field_validator("encoder")
    @classmethod
    def _encoder_is_av1(cls, value: str) -> str:
        """AV1 is the only output codec (issue #21): a two-value assertion,
        not a free choice. A config that still names x265 is rejected at load
        rather than silently producing H.265 while every doc says AV1."""
        if value not in OUTPUT_ENCODERS:
            raise ValueError(
                f"'{value}' is not an output codec; AV1 is the only one "
                f"(issue #21). valid: {', '.join(OUTPUT_ENCODERS)}"
            )
        return value

    @model_validator(mode="after")
    def _presets_in_vocabulary(self) -> "HandbrakeSection":
        for key, value in (
            ("probe_encoder_preset", self.probe_encoder_preset),
            ("encoder_preset", self.encoder_preset),
        ):
            vocabulary = ENCODER_PRESET_VOCABULARIES.get(self.encoder, ENCODER_PRESETS)
            if value not in vocabulary:
                raise ValueError(
                    f"handbrake.{key}: '{value}' is not a "
                    f"valid speed preset for encoder '{self.encoder}' "
                    f"(valid: {', '.join(vocabulary)})"
                )
        return self


class BlackBarsSection(BaseModel):
    """ffmpeg.black_bars: the minimum bar size cropdetect trusts."""

    model_config = ConfigDict(extra="forbid")

    MIN_WIDTH: int = Field(DEFAULT_BLACK_BAR_MIN_WIDTH, ge=0)
    MIN_HEIGHT: int = Field(DEFAULT_BLACK_BAR_MIN_HEIGHT, ge=0)


class FfmpegSection(BaseModel):
    """The ffmpeg section of config.json."""

    model_config = ConfigDict(extra="forbid")

    black_bars: BlackBarsSection = Field(default_factory=BlackBarsSection)
    crop_sample_count: int = Field(DEFAULT_CROP_SAMPLE_COUNT, ge=1)


class OutputSection(BaseModel):
    """file.output: the text-report separator.

    'seperator' is a deprecated alias kept for existing files; it logs one
    warning and the canonical key is 'separator'.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    separator: str = DEFAULT_SEPARATOR

    @model_validator(mode="before")
    @classmethod
    def _deprecated_seperator(cls, data: Any) -> Any:
        if isinstance(data, dict) and "seperator" in data:
            log_warning(
                "file.output.seperator is deprecated - rename it to "
                "'separator' (the old key is honoured for now)."
            )
            data = {**data}
            data.setdefault("separator", data.pop("seperator"))
        return data


class FileSection(BaseModel):
    """The file section of config.json."""

    model_config = ConfigDict(extra="forbid")

    extensions: list[str] = Field(DEFAULT_EXTENSIONS, min_length=1)
    output: OutputSection = Field(default_factory=OutputSection)


class QualitySection(BaseModel):
    """quality: the probe density knobs (issue #14)."""

    model_config = ConfigDict(extra="forbid")

    # the required VMAF every entry point uses unless a --target-vmaf flag
    # says otherwise (issue #252); part of the stored plan fingerprint
    target_vmaf: float = Field(DEFAULT_TARGET_VMAF, ge=1.0, le=100.0)
    sample_every: float = Field(DEFAULT_SAMPLE_EVERY, ge=0)
    sample_count: int = Field(DEFAULT_SAMPLE_COUNT, ge=1)
    sample_seconds: float = Field(DEFAULT_SAMPLE_SECONDS, gt=0)


class ResourcesSection(BaseModel):
    """resources: how politely the engine behaves on a shared machine."""

    model_config = ConfigDict(extra="forbid")

    idle_priority: StrictBool = DEFAULT_IDLE_PRIORITY


class EligibilitySection(BaseModel):
    """eligibility: which sources are ever worth re-encoding."""

    model_config = ConfigDict(extra="forbid")

    min_source_height: int = DEFAULT_MIN_SOURCE_HEIGHT
    min_source_width: int = DEFAULT_MIN_SOURCE_WIDTH
    min_source_size_gb: float = Field(DEFAULT_MIN_SOURCE_SIZE_GB, ge=0)
    # the operator's EXTRA codecs: empty is the normal case (issue #271)
    candidate_codecs: list[str] = DEFAULT_CANDIDATE_CODECS

    @field_validator("min_source_height")
    @classmethod
    def _floor_is_raise_only(cls, value: int) -> int:
        """The 1080p floor is a product rule, not a preference (issue #246):
        a config may raise it, never lower it or switch it off with 0."""
        if value < DEFAULT_MIN_SOURCE_HEIGHT:
            raise ValueError(
                f"{value} is below the {DEFAULT_MIN_SOURCE_HEIGHT}p floor; sources "
                f"below {DEFAULT_MIN_SOURCE_HEIGHT}p are never re-encoded, so the "
                f"setting may only raise it (e.g. 2160 for 4K only)"
            )
        return value

    @field_validator("min_source_width")
    @classmethod
    def _width_floor_is_raise_only(cls, value: int) -> int:
        """The 1920 width arm is the same 1080p-class rule (issue #233): a
        config may raise it, never lower it below the class floor."""
        if value < DEFAULT_MIN_SOURCE_WIDTH:
            raise ValueError(
                f"{value} is below the {DEFAULT_MIN_SOURCE_WIDTH}px floor; the "
                f"1080p class accepts either dimension at class size, so the "
                f"setting may only raise it"
            )
        return value

    @field_validator("candidate_codecs", mode="before")
    @classmethod
    def _lowercase_codecs(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [str(c).lower() for c in value]
        return value


class AppSection(BaseModel):
    """app: how the resident app behaves on boot."""

    model_config = ConfigDict(extra="forbid")

    auto_backlog: bool = False


class HdrSection(BaseModel):
    """codec.hdr: the markers that flag a source as HDR.

    The defaults ARE the documented HDR signature (issue #236, the
    maintainer's decision: HDR detection defaults in code): a config that
    says nothing about HDR still classifies bt2020nc/smpte2084 streams
    correctly, because the shipped config.json's values — the repo's own
    working setup — are now the code's defaults. An operator can still
    narrow or extend the lists; an empty list means "nothing is HDR".
    """

    model_config = ConfigDict(extra="forbid")

    color_space: list[str] = ["bt2020nc"]
    color_transfer: list[str] = ["smpte2084"]


class CodecSection(BaseModel):
    """The codec section of config.json."""

    model_config = ConfigDict(extra="forbid")

    hdr: HdrSection = Field(default_factory=HdrSection)


class ConfigModel(BaseModel):
    """The config.json schema (issue #61): one pydantic model instead of
    hand-written dict.get() fallbacks.

    Every mistake is an ERROR that names the key path (extra="forbid" at
    every level; strict types), never a silently-applied default. A MISSING
    file still means all defaults, as documented. The 'integrations' key is
    popped before validation with its removal warning (issue #43) rather
    than rejecting long-standing config files.
    """

    model_config = ConfigDict(extra="forbid")

    file: FileSection = Field(default_factory=FileSection)
    ffmpeg: FfmpegSection = Field(default_factory=FfmpegSection)
    handbrake: HandbrakeSection = Field(default_factory=HandbrakeSection)
    quality: QualitySection = Field(default_factory=QualitySection)
    resources: ResourcesSection = Field(default_factory=ResourcesSection)
    codec: CodecSection = Field(default_factory=CodecSection)
    app: AppSection = Field(default_factory=AppSection)
    eligibility: EligibilitySection = Field(default_factory=EligibilitySection)
    language: str = DEFAULT_LANGUAGE


class Conf:
    """Provides typed access to the config.json settings.

    Validated against ConfigModel (issue #61): every value falls back to the
    model's default when missing, and every mistake - an unknown key, a
    string where a bool belongs, an out-of-range number - is an error that
    names the key path, never a silently-applied default.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = dict(data or {})
        # the integrations key (opt-in media-server hooks) was REMOVED with the
        # Jellyfin integration (issue #43): an operator who relied on the
        # refresh must see that it is gone, so a leftover key is named - once,
        # honestly, and without failing the load
        if "integrations" in data:
            log_warning(
                "config.json contains 'integrations' - the media-server "
                "integration was removed (issue #43); the key is ignored."
            )
            data.pop("integrations")

        model = ConfigModel.model_validate(data)

        # codec => hdr detection
        self.hdr_color_spaces: list[str] = model.codec.hdr.color_space
        self.hdr_color_transfers: list[str] = model.codec.hdr.color_transfer

        # file => extensions and output settings
        self.extensions: list[str] = model.file.extensions
        self.separator: str = model.file.output.separator

        # ffmpeg => black bar detection + crop detection
        self.black_bar_min_width: int = model.ffmpeg.black_bars.MIN_WIDTH
        self.black_bar_min_height: int = model.ffmpeg.black_bars.MIN_HEIGHT
        self.crop_sample_count: int = model.ffmpeg.crop_sample_count

        self.language: str = model.language

        # handbrake => re-encoding settings
        self.handbrake_preset: str = model.handbrake.preset
        self.handbrake_encoder: str = model.handbrake.encoder
        # quality mode's TWO encoder speed presets (issue #22): the probe
        # preset drives the CRF search's throwaway measurement encodes, the
        # final preset drives the kept artifact - measurement at 'medium',
        # artifact at 'slow' by default. The probe is a PREDICTION of the
        # final encode: preset shifts the CRF-to-VMAF curve, so a probe at a
        # different preset than the final encode measures a different curve
        # - a deliberate trade-off (documented in the README), not a bug.
        self.handbrake_probe_encoder_preset: str = model.handbrake.probe_encoder_preset
        self.handbrake_encoder_preset: str = model.handbrake.encoder_preset
        self.handbrake_max_threads: int = model.handbrake.max_threads
        self.handbrake_audio_languages: list[str] = model.handbrake.audio_languages
        self.handbrake_subtitle_languages: list[str] = model.handbrake.subtitle_languages
        # stall watchdog: minutes without child output before a hung
        # HandBrake/ffmpeg is terminated (0 = disabled; default 10)
        self.handbrake_stall_minutes: float = model.handbrake.stall_minutes

        # quality => probe density
        self.sample_every: float = model.quality.sample_every
        self.sample_count: int = model.quality.sample_count
        self.sample_seconds: float = model.quality.sample_seconds
        self.target_vmaf: float = model.quality.target_vmaf

        # resources => how politely the engine behaves on a shared machine
        self.idle_priority: bool = model.resources.idle_priority

        # eligibility => sources below this height/width/size are never
        # re-encoded; the codec whitelist is extended, never shrunk
        self.min_source_height: int = model.eligibility.min_source_height
        self.min_source_width: int = model.eligibility.min_source_width
        self.min_source_size_gb: float = model.eligibility.min_source_size_gb
        self.candidate_codecs: list[str] = model.eligibility.candidate_codecs

        # app => whether the resident app starts the backlog on boot
        self.auto_backlog: bool = model.app.auto_backlog


_config: Conf | None = None

# the config file's resolved path (set by the last load; surfaced for logs
# and the dashboard's "which file is in effect" display)
config_file_used: Path | None = None

# what refresh_config() last judged (issue #247): the file's (mtime_ns, size),
# or None when it did not exist. A changed signature means "read it again".
_config_signature: tuple[int, int] | None = None

# why the file on disk is NOT the config in effect (an edit that failed to
# parse or validate), or None when it is. The dashboard's status shows it.
config_error: str | None = None

# the watcher thread and the job worker both refresh between files
_refresh_lock = threading.Lock()

CONFIG_ENV_VAR = "SHRINKRAY_CONFIG"

# the defaults to create $SHRINKRAY_CONFIG from when it does not exist yet
# (issue #247): the image points it at its baked config, so the first start
# writes the operator's own, persistent /data/config.json
CONFIG_SEED_ENV_VAR = "SHRINKRAY_CONFIG_SEED"

DEFAULT_CONFIG_FILENAME = "config.json"


def resolve_config_path(
    config_file: str | Path | None = None,
    root: str | Path | None = None,
    db_dir: str | Path | None = None,
) -> Path:
    """Resolves which config.json is in effect (issue #58).

    The location is decided per load, in this order:

    1. an explicit config FILE path (the CLI's --config option),
    2. an explicit root DIRECTORY (tests, the app's db-dir convention),
    3. the SHRINKRAY_CONFIG environment variable (the container/NAS
       mount point),
    4. db_dir/config.json - the directory the operator already chose for
       user data,
    5. ./config.json (the working directory; the default --db lives there
       too, so a repo-root run reads the repo's config.json).

    Package-relative lookup is gone deliberately: in an installed wheel the
    directory above site-packages is not a user-writable location, and the
    dashboard and the engine must agree on one file.

    Args:
        config_file (str | Path | None): Explicit config FILE path.
        root (str | Path | None): Explicit directory holding config.json.
        db_dir (str | Path | None): The database file's directory (the
            operator's chosen user-data location).

    Returns:
        Path: The config file path in effect.
    """
    if config_file is not None:
        return Path(config_file)
    if root is not None:
        return Path(root) / DEFAULT_CONFIG_FILENAME
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env)
    if db_dir is not None:
        return Path(db_dir) / DEFAULT_CONFIG_FILENAME
    return Path(DEFAULT_CONFIG_FILENAME)


def reset_config() -> None:
    """Drops the cached config so the next load_config() reads the file.

    The public way for the dashboard's PUT /api/config to swap the
    in-memory singleton (assigning config._config from outside the module
    reached into private state; issue #58).
    """
    global _config, _config_signature, config_error
    _config = None
    _config_signature = None
    config_error = None


def _file_signature(path: Path) -> tuple[int, int] | None:
    """Returns a file's (mtime_ns, size), or None when it does not exist."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def refresh_config() -> Conf:
    """Returns the config, re-read first when its file changed on disk.

    A long-running process applies a saved change at its next file (issue
    #247): the watcher and the job worker call this between files, so a
    running encode finishes with the settings it started with. Re-reading
    costs one stat when nothing changed.

    An edit that does not parse or validate never replaces a working config:
    the previous settings stay in effect, the reason is logged once and kept
    in `config_error` for the dashboard, and the next change is judged again.
    A deleted file is the same kind of error, never a silent fall-back to the
    code defaults.

    Returns:
        Conf: The config in effect.
    """
    global _config, _config_signature, config_error
    with _refresh_lock:
        if _config is None or config_file_used is None:
            return load_config()
        path = config_file_used
        signature = _file_signature(path)
        if signature == _config_signature:
            return _config
        # this version of the file is judged now, good or bad: a bad one is
        # reported once, not on every file boundary
        _config_signature = signature
        try:
            if signature is None:
                raise FileNotFoundError("the file no longer exists")
            data = json.loads(path.read_text(encoding="utf-8"))
            fresh = Conf(data)
        except (OSError, ValueError) as e:
            # ValueError covers json's decode error and pydantic's
            # ValidationError (a ValueError subclass)
            config_error = f"'{path}' was not applied, the previous settings stay in effect: {e}"
            log_error(config_error)
            return _config
        _config = fresh
        config_error = None
        log(f"Config reloaded from {path}")
        return _config


def seed_config_file(target: Path, seed: Path) -> bool:
    """Creates `target` as a copy of `seed` unless it already exists.

    Never overwrites: an existing file is the operator's. The copy is
    written to a temp file beside the target and hard-linked into place,
    which fails if the target appeared meanwhile, so a crash or a second
    process can never leave a half-written or replaced config. The copy
    takes the seed's permission bits, not the temp file's private 0600, so
    the operator can edit it from wherever they reach the folder.

    Args:
        target (Path): The config file to create.
        seed (Path): The defaults to copy.

    Returns:
        bool: True when the file was created, False when it already existed.

    Raises:
        OSError: When the seed cannot be read or the target not written.
    """
    if target.exists():
        return False
    content = seed.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        shutil.copymode(seed, temp_name)
        try:
            os.link(temp_name, target)
        except FileExistsError:
            return False
    finally:
        os.unlink(temp_name)
    return True


def seed_config_from_env() -> None:
    """Creates $SHRINKRAY_CONFIG from $SHRINKRAY_CONFIG_SEED when it is missing.

    The container sets both (issue #247), so the first command it runs
    writes the operator's persistent `/data/config.json` from the image's
    defaults. Natively neither is set and this does nothing. A failure is
    reported, not raised: the command still runs, on the code defaults.
    """
    target = os.environ.get(CONFIG_ENV_VAR)
    seed = os.environ.get(CONFIG_SEED_ENV_VAR)
    if not target or not seed:
        return
    try:
        created = seed_config_file(Path(target), Path(seed))
    except OSError as e:
        log_error(
            f"Could not create '{target}' from '{seed}': {e}. Settings fall back to the "
            "code defaults and will not persist until the folder is writable."
        )
        return
    if created:
        log(f"Created '{target}' from the defaults in '{seed}'; edit it to change settings.")


def load_config(
    root: str | Path | None = None,
    config_file: str | Path | None = None,
) -> Conf:
    """Loads the config.json file and returns the settings.

    The result is cached for the process (one file in effect at a time), with one
    exception: an explicit `config_file` always wins, so a caller that names a
    file gets that file's settings even when a config is already cached (issue
    #328: --config was silently ignored).

    If the config file does not exist, default values are used. The file's
    location follows resolve_config_path; the resolved path is logged once
    at load so a user can see which file is in effect.

    Args:
        root (str | Path | None): Explicit directory holding config.json
            (the app passes its db directory; tests pass temp dirs).
        config_file (str | Path | None): Explicit config FILE path (the
            CLI's --config option; wins over everything else).

    Returns:
        Conf: The config settings.
    """
    global _config, config_file_used, _config_signature
    # an EXPLICIT config_file is a command-line instruction, not a cache hint:
    # --config must win over a config another caller loaded first (issue #328's
    # fallback test caught this; the CLI silently used the repo's ./config.json)
    explicit = config_file is not None
    if _config is not None and not explicit:
        return _config

    path = resolve_config_path(config_file=config_file, root=root)
    if _config is not None and path == config_file_used:
        return _config

    data: dict[str, Any] = {}
    signature = _file_signature(path)
    if path.is_file():
        with open(path, encoding="utf-8") as config:
            data = json.load(config)
        log(f"Using config {path}")
    else:
        log_warning(f"Config file not found at '{path}', using default values!")

    # validate BEFORE recording which file is in effect: Conf(data) raises on a bad
    # file, and assigning config_file_used first would leave the cache pointing at
    # a file whose settings are not in effect (refresh_config would then watch the
    # rejected file, and a retry would silently return the stale settings).
    fresh = Conf(data)
    config_file_used = path
    _config = fresh
    _config_signature = signature
    return _config
