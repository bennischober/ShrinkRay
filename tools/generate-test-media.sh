#!/usr/bin/env bash
# Generates the synthetic boundary fixtures for manual E2E runs and the
# opt-in smoke tests (issue #334).
#
# Why: the only test material used to be three full movies (31-70 GB), so
# every real-surface run of plan/encode cost hours and the gates' BOUNDARIES
# (duration floor, size floor, resolution floor, codec whitelist, bar
# detection) had no fast fixture to exercise them. The fixtures here are
# tiny (< 50 MB each), deterministically generated with lavfi, and cover
# one boundary each:
#
#   bars-1080p.mkv    1080p h264, real pillarbox bars, heavy noise -> the
#                     full happy path: gates pass, bars detected, AV1
#                     encode should shrink it (~300 s, ~25 MB)
#   short.mkv         60 s 1080p h264 -> BELOW the 90 s duration floor:
#                     plan must skip with the duration verdict
#   small-720p.mkv    720p h264, long enough and big enough -> BELOW the
#                     1080p class floor: plan must skip on resolution
#   old-codec.mkv     mpeg2video 1080p -> OUTSIDE the codec whitelist:
#                     plan must skip on codec
#   tiny-size.mkv     1080p h264, 3 min of near-black -> a few MB, for
#                     testing an explicit min_source_size_gb floor
#
# Usage:  tools/generate-test-media.sh [target-dir]
#         (default: test_media/synthetic, created if missing)
#
# Needs ffmpeg on PATH with the lavfi sources (any distro build; no
# libvmaf needed to GENERATE). The files are gitignored like the rest of
# test_media/ - never commit them.

set -euo pipefail

TARGET="${1:-test_media/synthetic}"
mkdir -p "$TARGET"

have() { command -v "$1" >/dev/null 2>&1; }
have ffmpeg || { echo "ffmpeg not found on PATH" >&2; exit 1; }

gen() {
  local out="$1"; shift
  if [ -f "$TARGET/$out" ]; then
    echo "exists: $out (delete it to regenerate)"
    return 0
  fi
  echo "generating $out ..."
  ffmpeg -hide_banner -loglevel error -y "$@" "$TARGET/$out"
}

# 1. bars-1080p.mkv — the happy-path fixture: 1080p h264 with real 160 px
#    pillarbox bars, gentle motion (testsrc's moving box), 2 min, stereo
#    AAC. Gentle motion keeps the x264 source small while AV1's edge on
#    synthetic content still gives the size gate room to pass.
gen "bars-1080p.mkv" \
  -f lavfi -i "testsrc=size=1600x1080:rate=30:duration=120" \
  -f lavfi -i "sine=frequency=440:duration=120" \
  -f lavfi -i "sine=frequency=620:duration=120" \
  -filter_complex "[0:v]pad=1920:1080:(ow-iw)/2:0:black[v];[1:a][2:a]amix=inputs=2[a]" \
  -map "[v]" -map "[a]" \
  -c:v libx264 -preset medium -crf 28 -pix_fmt yuv420p \
  -c:a aac -b:a 128k

# 2. short.mkv — below the duration floor (MIN_ENCODE_SECONDS = 90).
gen "short.mkv" \
  -f lavfi -i "testsrc=size=1920x1080:rate=30:duration=60" \
  -f lavfi -i "sine=frequency=440:duration=60" \
  -map 0:v -map 1:a \
  -c:v libx264 -preset medium -crf 28 -pix_fmt yuv420p \
  -c:a aac -b:a 96k

# 3. small-720p.mkv — below the 1080p class (neither width >= 1920 nor
#    height >= 1080), long enough to pass the duration gate.
gen "small-720p.mkv" \
  -f lavfi -i "testsrc=size=1280x720:rate=30:duration=120" \
  -f lavfi -i "sine=frequency=440:duration=120" \
  -map 0:v -map 1:a \
  -c:v libx264 -preset medium -crf 28 -pix_fmt yuv420p \
  -c:a aac -b:a 96k

# 4. old-codec.mkv — outside the codec whitelist (mpeg2video), 1080p at a
#    modest bitrate so the file stays small.
gen "old-codec.mkv" \
  -f lavfi -i "testsrc=size=1920x1080:rate=30:duration=120" \
  -f lavfi -i "sine=frequency=440:duration=120" \
  -map 0:v -map 1:a \
  -c:v mpeg2video -b:v 1200k -pix_fmt yuv420p \
  -c:a mp2 -b:a 96k

# 5. tiny-size.mkv — 1080p but nearly empty (near-black): a few MB, for
#    exercising an explicit min_source_size_gb floor.
gen "tiny-size.mkv" \
  -f lavfi -i "color=c=black:s=1920x1080:rate=30:duration=120" \
  -f lavfi -i "sine=frequency=440:duration=120" \
  -map 0:v -map 1:a \
  -c:v libx264 -preset medium -crf 34 -pix_fmt yuv420p \
  -c:a aac -b:a 64k

echo "done: $TARGET"
ls -la "$TARGET"
