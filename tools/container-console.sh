# Sourced by every interactive shell in the container image (sh via $ENV, bash
# via /etc/bash.bashrc), issue #248. A console opened as root - Unraid's Console
# button runs `docker exec -it <container> sh` - becomes the operator's user, so
# nothing typed there writes to the share as root. Set SHRINKRAY_ROOT_SHELL=1 on
# the `docker exec` to keep root for maintenance.
if [ "$(id -u)" = 0 ] && [ -z "${SHRINKRAY_ROOT_SHELL:-}" ]; then
    if [ -n "${BASH_VERSION:-}" ]; then
        exec setpriv --reuid="$(id -u shrinkray)" --regid="$(id -g shrinkray)" --init-groups \
            env HOME=/home/shrinkray /bin/bash
    fi
    exec setpriv --reuid="$(id -u shrinkray)" --regid="$(id -g shrinkray)" --init-groups \
        env HOME=/home/shrinkray /bin/sh
fi
umask "${UMASK:-022}"
