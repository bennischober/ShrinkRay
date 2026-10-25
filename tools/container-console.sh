# Sourced by every interactive shell in the container image (sh via $ENV, bash
# via /etc/bash.bashrc), issues #248 and #251. A console opened as root - Unraid's Console
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

# the operator's manual, once per console (issue #251): a nested `sh` typed
# inside it inherits the variable and stays quiet
if [ -z "${SHRINKRAY_BANNER_SHOWN:-}" ]; then
    export SHRINKRAY_BANNER_SHOWN=1
    python /app/tools/console_banner.py ||
        echo "ShrinkRay: the console banner failed (see above); run shrinkray --help"
fi
