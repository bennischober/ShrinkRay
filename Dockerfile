# ShrinkRay's container image (issue #34): one Dockerfile, two targets.
#
# `runtime` is the deployment shape - a container on a server, pointed at a
# media folder, running unattended. `dev` is the same base plus the dev group
# and Node, so the five gate commands run in the environment they ship in.
#
# The toolchain is pinned by version AND sha256, never a distro package: stock
# Debian/Ubuntu ffmpeg is built without libvmaf, so quality mode cannot run on
# it at all, and an unpinned version makes the stored VMAF scores
# incomparable across machines (libvmaf v2 scored 95.76 where v3 scored 94.40 on
# the same encode).

# ── stage 1: the built web UI ────────────────────────────────────────────
# The app serves ui/dist from the checkout root (app.py's _ui_dist), so the
# image needs the built assets rather than a wheel that ships only src/.
FROM node:24-bookworm-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# ── stage 2: the pinned media toolchain ──────────────────────────────────
# ffmpeg must come from a build with libvmaf; HandBrake ships no Linux CLI
# binary, so it is compiled. Both are pinned by version + sha256 and the build
# fails if either version string does not match the pin - an image that silently
# measured with a different libvmaf would write incomparable scores.
FROM debian:bookworm-slim AS toolchain

ARG FFMPEG_VERSION=autobuild-2026-10-23-13-04
ARG FFMPEG_ASSET=ffmpeg-N-127957-g935f29eaae-linux64-gpl.tar.xz
ARG FFMPEG_SHA256=1eae7b6b6622dbda60b30c7b2f486224a6ea9980f2a4c2d2543eee4c8d269baa
ARG HANDBRAKE_VERSION=1.11.2
ARG HANDBRAKE_SHA256=12b046350f2422dc28783ff94229aff4ba5fe5e683431e057355d36163b2593a

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      autoconf automake build-essential ca-certificates cmake curl xz-utils \
      libtool libtool-bin m4 make meson ninja-build patch pkg-config tar \
      libass-dev libbluray-dev libbz2-dev libdvdnav-dev libdvdread-dev \
      libfribidi-dev libjansson-dev liblzma-dev libmp3lame-dev \
      libnuma-dev libogg-dev libopus-dev libsamplerate0-dev libspeex-dev \
      libtheora-dev libturbojpeg0-dev libvorbis-dev libvpx-dev \
      libx264-dev libx265-dev nasm zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# ffmpeg: downloaded at the pinned tag, verified against the pinned digest.
RUN curl -fsSL -o /tmp/ffmpeg.tar.xz \
      "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_VERSION}/${FFMPEG_ASSET}" \
 && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
 && mkdir -p /opt/ffmpeg \
 && tar -xJf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1 \
 && rm /tmp/ffmpeg.tar.xz \
 && ln -s /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg \
 && ln -s /opt/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe \
 && ffmpeg -hide_banner -h filter=libvmaf >/dev/null \
 && ffmpeg -version | head -1 | grep -q "N-127957-g935f29eaae"

# HandBrakeCLI: no Linux binary is published, so it is compiled from the
# pinned source. The version check is the gate: a mis-built binary must fail the
# build rather than produce silently incomparable scores.
RUN curl -fsSL -o /tmp/hb.tar.bz2 \
      "https://github.com/HandBrake/HandBrake/releases/download/${HANDBRAKE_VERSION}/HandBrake-${HANDBRAKE_VERSION}-source.tar.bz2" \
 && echo "${HANDBRAKE_SHA256}  /tmp/hb.tar.bz2" | sha256sum -c - \
 && mkdir -p /tmp/hb && tar -xjf /tmp/hb.tar.bz2 -C /tmp/hb --strip-components=1 \
 && cd /tmp/hb \
 && ./configure --disable-gtk --disable-docs \
 && make -C build -j"$(nproc)" \
 && make -C build install \
 && cd / && rm -rf /tmp/hb /tmp/hb.tar.bz2 \
 && HandBrakeCLI --version | grep -q "${HANDBRAKE_VERSION}"

# ── stage 3: the app - the runtime image (and the dev base) ───────────
FROM debian:bookworm-slim AS runtime

# uv installs the interpreter .python-version names, so the image is not tied
# to a python:3.x-<distro> tag existing for this Debian release.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libgomp1 libnuma1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=toolchain /opt/ffmpeg /opt/ffmpeg
COPY --from=toolchain /usr/local/bin/HandBrakeCLI /usr/local/bin/HandBrakeCLI
RUN ln -s /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg \
 && ln -s /opt/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe

# The toolchain links these; the runtime stage carries them, not the build deps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libass9 libbluray2 libdvdnav4 libdvdread8 libjansson4 libmp3lame0 libnuma1 \
      libogg0 libopus0 libsamplerate0 libspeex1 libtheora0 libturbojpeg0 \
      libvorbis0a libvorbisenc2 libvpx7 libx264-164 libx265-199 zlib1g \
 && rm -rf /var/lib/apt/lists/*

# non-root by default: everything the app writes on a bind mount must be
# host-owned, and a root-owned output file is a support ticket on the NAS
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" shrinkray \
 && useradd -u "${UID}" -g "${GID}" -m -s /usr/sbin/nologin shrinkray

# uv installs the interpreter under its own home by default, which a non-root
# user cannot read: point it at a shared path so the venv is usable by both.
ENV UV_PYTHON_INSTALL_DIR=/opt/python

WORKDIR /app
COPY --chown=shrinkray:shrinkray . /app
COPY --from=ui --chown=shrinkray:shrinkray /ui/dist /app/ui/dist

RUN uv sync --frozen --no-dev \
 && chown -R shrinkray:shrinkray /app/.venv

# The app resolves ui/dist from the checkout root, which is /app here; the DB,
# config and probe scratch all need writable paths that are volumes, not layers.
RUN mkdir -p /data /media /output /tmp/shrinkray \
 && chown -R shrinkray:shrinkray /data /output /tmp/shrinkray

ENV TMPDIR=/tmp/shrinkray \
    PATH="/app/.venv/bin:${PATH}"

USER shrinkray

# the container's config: a tracked file, COPYed (not printf-baked — the
# JSON in shell escapes was unreadable and untestable, and #206's test had to
# parse it back out of this Dockerfile). auto_backlog is the fire-and-forget
# contract (issue #126) - a fresh container scans, plans and encodes the
# library with no client. The key is off by default outside the image, so a
# local dev run never starts encoding.
COPY container-config.json /app/config.json

# `shrinkray` is the entrypoint so the image runs either mode (issue #242): no
# arguments start the resident server below, any arguments are one CLI command,
# e.g. `docker run --rm -v lib:/media <image> encode /media/film.mkv`.
# --root is not optional on Linux: the default allowlist is the current
# directory when no drive root exists, so without it every library path is a 403.
# serve is the resident mode's name (issue #202); `app` stays a hidden alias.
ENTRYPOINT ["shrinkray"]
CMD ["serve", "--db", "/data/metadata.db", "--host", "0.0.0.0", "--root", "/media", "--config", "/app/config.json"]

EXPOSE 8420

# /api/status is the app's own summary; no new endpoint is needed. The script
# sends the token (the bind is non-loopback, issue #36) and reports a one-shot
# CLI container as healthy, since only the server has a status to check (#242).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "/app/tools/healthcheck.py"]

# ── stage 4: the dev target (the gate runs inside it) ─────────────────────
FROM runtime AS dev

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN uv sync --frozen \
 && chown -R shrinkray:shrinkray /app/.venv

USER shrinkray

# the gate writes caches, and /app belongs to root at build time: point every
# cache at writable scratch rather than chowning the checkout, which would hide
# a stray artifact inside the image.
ENV RUFF_CACHE_DIR=/tmp/ruff-cache \
    MYPY_CACHE_DIR=/tmp/mypy-cache \
    COVERAGE_FILE=/tmp/.coverage \
    PYTEST_ADDOPTS="-p no:cacheprovider"

ENTRYPOINT []
CMD ["/app/tools/gate.sh"]
