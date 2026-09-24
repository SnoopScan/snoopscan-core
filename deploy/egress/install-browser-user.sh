#!/usr/bin/env bash
#
# Give the browsers their own user, so the crawler's loopback exceptions do
# not apply to them. FOR REVIEW — run it deliberately, as root.
#
# Without this the browsers share the engine's uid, and the egress filter
# cannot tell the engine reaching its own database from a page persuading
# Chromium to open 127.0.0.1:5432. They are the same identity to the kernel.
#
#   sudo ./install-browser-user.sh            install
#   sudo ./install-browser-user.sh --remove   undo it completely
set -euo pipefail

ENGINE_USER=snoopscan
BROWSER_USER=snoopbrowser
SHARED_GROUP=snoopbrowse
HOME_DIR=/var/lib/snoopbrowser
WRAPPER=/usr/local/bin/snoopscan-browser
SUDOERS=/etc/sudoers.d/snoopscan-browser
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

if [ "${1:-}" = "--remove" ]; then
    rm -f "$SUDOERS" "$WRAPPER"
    userdel "$BROWSER_USER" 2>/dev/null || true
    groupdel "$SHARED_GROUP" 2>/dev/null || true
    rm -rf "$HOME_DIR"
    echo "removed. Unset ENGINE_BROWSER_EXECUTABLE and restart the API."
    exit 0
fi

CHROME=$(su -s /bin/bash - "$ENGINE_USER" -c \
    'cd /srv/snoopscan && .venv/bin/python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p: print(p.chromium.executable_path)" 2>/dev/null' | tail -1)
[ -x "$CHROME" ] || { echo "could not find the browser binary (got: ${CHROME:-nothing})" >&2; exit 1; }
echo "browser binary: $CHROME"

id "$BROWSER_USER" >/dev/null 2>&1 || \
    useradd --system --no-create-home --shell /usr/sbin/nologin "$BROWSER_USER"
groupadd -f "$SHARED_GROUP"
usermod -aG "$SHARED_GROUP" "$ENGINE_USER"
usermod -aG "$SHARED_GROUP" "$BROWSER_USER"
install -d -o "$BROWSER_USER" -g "$SHARED_GROUP" -m 0750 "$HOME_DIR"
usermod -d "$HOME_DIR" "$BROWSER_USER"

# The browser must be able to read its own install tree.
chmod -R o+rX "$(dirname "$(dirname "$CHROME")")" 2>/dev/null || true

install -m 0755 "$HERE/snoopscan-browser" "$WRAPPER"
sed -i "s#^CHROME=.*#CHROME=$CHROME#" "$WRAPPER"

cat > "$SUDOERS" <<SUDO
# The crawler may run ONLY the browser binary, and only as $BROWSER_USER.
# closefrom_override: Playwright hands the browser its remote-debugging pipe
# on fds 3 and 4, and sudo closes everything above 2 by default.
Defaults!SNOOPBROWSER closefrom_override
Cmnd_Alias SNOOPBROWSER = $CHROME
$ENGINE_USER ALL=($BROWSER_USER) NOPASSWD: SNOOPBROWSER
SUDO
chmod 0440 "$SUDOERS"
visudo -c -f "$SUDOERS" >/dev/null || { echo "sudoers rule is invalid" >&2; rm -f "$SUDOERS"; exit 1; }

echo
echo "Installed. Now point the engine at it and restart:"
echo "  echo 'ENGINE_BROWSER_EXECUTABLE=$WRAPPER' >> /srv/snoopscan/.env"
echo "  systemctl restart snoopscan-api"
echo
echo "Then confirm the browsers really run as $BROWSER_USER:"
echo "  pgrep -u $BROWSER_USER -f chrome | wc -l     # >0 during a browser scrape"
