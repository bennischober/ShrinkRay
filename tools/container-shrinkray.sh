#!/bin/sh
# ShrinkRay inside its container image: every command runs as the operator's
# user (issue #248). Installed under two names:
#
#   shrinkray-entrypoint  the image's ENTRYPOINT. Once per container start, as
#                         root: gives the image's user the operator's PUID and
#                         PGID, hands it the image-owned folders, refuses to
#                         start when a folder it must write is not writable,
#                         then runs the command as that user.
#   shrinkray             first on PATH. Every command - the server, a one-shot
#                         run, a `docker exec ... shrinkray ...` - runs as that
#                         user with UMASK applied, never as root.
#
# A mounted library (/media) is never chowned: its ownership is the operator's.
# /data, /output and the scratch folder are chowned (the folder itself, never
# recursively) only while they are the image's own or hold nothing, which is how
# a fresh appdata folder arrives: Docker creates a missing host folder owned by
# root.
set -eu

REAL=/app/.venv/bin/shrinkray
USER_NAME=shrinkray
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"

fail() {
    echo "shrinkray: $*" >&2
    exit 1
}

as_user() {
    setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
}

is_empty_dir() {
    [ -z "$(ls -A "$1" 2>/dev/null)" ]
}

# a folder the app writes: take it over while it is the image's own or empty,
# otherwise it must already be writable by the operator's user
claim_dir() {
    dir="$1"
    [ -d "$dir" ] || return 0
    if ! mountpoint -q "$dir" || is_empty_dir "$dir"; then
        chown "$PUID:$PGID" "$dir"
    fi
    as_user test -w "$dir" || fail "'$dir' is not writable by uid $PUID / gid $PGID. \
Make the folder you mounted there writable for that user (e.g. chown -R $PUID:$PGID <host folder>), \
or set PUID/PGID to the user that owns it."
}

setup() {
    case "$PUID" in '' | *[!0-9]*) fail "PUID='$PUID' is not a numeric user id" ;; esac
    case "$PGID" in '' | *[!0-9]*) fail "PGID='$PGID' is not a numeric group id" ;; esac
    case "$UMASK" in '' | *[!0-7]*) fail "UMASK='$UMASK' is not an octal mask (e.g. 022)" ;; esac
    [ "$PUID" != 0 ] || fail "PUID=0 would run ShrinkRay as root; set it to the user that owns the library"

    # -o: the id may already belong to another account (Unraid's 100 is `users`)
    groupmod -o -g "$PGID" "$USER_NAME"
    usermod -o -u "$PUID" -g "$PGID" "$USER_NAME"

    claim_dir /data
    claim_dir /tmp/shrinkray
    claim_dir /output
    # the image's own empty /media (nothing mounted) is not anyone's library
    if [ -d /media ] && ! mountpoint -q /media; then
        chown "$PUID:$PGID" /media
    fi
    # the resident server encodes in place unless $SHRINKRAY_WATCH_OUTPUT points
    # elsewhere (the app validates that path itself at boot, issue #193)
    case "${1:-}" in
        serve | app)
            if [ -z "${SHRINKRAY_WATCH_OUTPUT:-}" ] && [ -d /media ]; then
                as_user test -w /media || fail "'/media' is not writable by uid $PUID / gid $PGID, \
but the server encodes in place. Set PUID/PGID to the user that owns the library, or point \
SHRINKRAY_WATCH_OUTPUT at a writable folder."
            fi
            ;;
    esac
}

if [ "$(basename "$0")" = shrinkray-entrypoint ] && [ "$(id -u)" = 0 ]; then
    setup "$@"
fi

umask "$UMASK"
if [ "$(id -u)" = 0 ]; then
    PUID="$(id -u "$USER_NAME")"
    PGID="$(id -g "$USER_NAME")"
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups \
        env HOME="/home/$USER_NAME" "$REAL" "$@"
fi
exec "$REAL" "$@"
