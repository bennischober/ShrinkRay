# the engine's live-progress callback: stage detail and counters, never persisted
# while encoding, free space below this floor trips the disk watchdog: the
# encode is cancelled (partial output removed) instead of filling the shared
# drive - a full disk harms every co-tenant, not just this run
# transitional re-exports (issue #97 phase 3): the encode pipeline
# moved to engine/pipeline.py; removed when import-linter lands (phase 6)
from shrinkray.engine.pipeline import (
    DISK_FREE_FLOOR_BYTES as DISK_FREE_FLOOR_BYTES,
)
from shrinkray.engine.pipeline import (
    EncodeResult as EncodeResult,
)
from shrinkray.engine.pipeline import (
    ProgressCallback as ProgressCallback,
)
from shrinkray.engine.pipeline import (
    _encode_pending as _encode_pending,
)
from shrinkray.engine.pipeline import (
    _encode_with_command as _encode_with_command,
)
from shrinkray.engine.pipeline import (
    _report_encode as _report_encode,
)
from shrinkray.engine.pipeline import (
    _terminate as _terminate,
)
from shrinkray.engine.pipeline import (
    cap_threads as cap_threads,
)
from shrinkray.engine.pipeline import (
    check_disk_space as check_disk_space,
)
from shrinkray.engine.pipeline import (
    disk_free_bytes as disk_free_bytes,
)
from shrinkray.engine.pipeline import (
    durations_match as durations_match,
)
from shrinkray.engine.pipeline import (
    encode_file as encode_file,
)
from shrinkray.engine.pipeline import (
    encode_movies as encode_movies,
)
from shrinkray.tools.ffprobe import (
    probe_duration_seconds as probe_duration_seconds,
)
from shrinkray.tools.handbrake import (
    HANDBRAKE_BINARY as HANDBRAKE_BINARY,
)
from shrinkray.tools.handbrake import (
    build_command as build_command,
)
from shrinkray.tools.handbrake import (
    find_handbrake as find_handbrake,
)
from shrinkray.tools.process import (
    DEFAULT_STALL_MINUTES as DEFAULT_STALL_MINUTES,
)
from shrinkray.tools.process import (
    configure_stall as configure_stall,
)
from shrinkray.tools.process import (
    stall_timeout_seconds as stall_timeout_seconds,
)
