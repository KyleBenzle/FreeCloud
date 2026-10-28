#!/usr/bin/env bash
set -Eeuo pipefail

# Root-side installer, staged by the laptop into its own timestamped directory.
# It only replaces the six FreeCloud PHP source files and the Pi CLI client.
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 2
fi

STAGE="$(cd -- "$(dirname -- "$0")" && pwd -P)"
STAMP="$(basename -- "$STAGE")"
WEB_ROOT=/var/www/kylebenzle.com/public/FreeCloud
PI_ROOT=/home/kbpi/FreeCloud
BACKUP_ROOT="/var/backups/freecloud/$STAMP"
PHP_FILES=(freecloud_api.php freecloud.php freecloud_transfer.php freecloud_download.php freecloud_preview.php freecloud_lifecycle.php)

declare -A EXPECTED=(
    [freecloud_api.php]=102bc22d8e6b6f0e87df078c53d6dd26c01203ce4aaf7a28b91e2da6485635c5
    [freecloud.php]=2efa26574e8d974682c9f2b53645abd1d7815966ac8814ecb1b8bf762ce93739
    [freecloud_transfer.php]=ddb9e3ea58e69b678aa772e21bf9c4f81218a742fe88281e2b6a6c11f4ff2b4c
    [freecloud_download.php]=382bffcd321415385660948e389f90633772d2bf1b08abec53971cd7b8d482c2
    [freecloud_preview.php]=66d995d45162b35f843ee32b12d6fae1138d4a0efc6d5ab05193f9fe18260e11
    [freecloud_lifecycle.php]=fd8135bc65665a7b6170148d2347b8c53086f5ac6838bb8015d8fb131c80dfda
    [freecloud_cli.py]=dea83801bbe6da19f2b8e30a0c469b5871263b2cc8f7e2fb1f22f3673a224c8a
)

for file in "${!EXPECTED[@]}"; do
    [[ -f "$STAGE/$file" ]] || { echo "Missing staged file: $file" >&2; exit 3; }
    actual="$(sha256sum "$STAGE/$file" | awk '{print $1}')"
    [[ "$actual" == "${EXPECTED[$file]}" ]] || { echo "Staged checksum mismatch: $file" >&2; exit 3; }
done

# Catch incomplete API/helper bundles before backing up or replacing production files.
php -r 'require $argv[1]; foreach (["fc_generation_allows", "fc_tombstone_for_path", "fc_make_global_delete_job"] as $name) { if (!function_exists($name)) { fwrite(STDERR, "Missing lifecycle helper: $name\\n"); exit(1); } }' "$STAGE/freecloud_lifecycle.php"

[[ -d "$WEB_ROOT" && ! -L "$WEB_ROOT" ]] || { echo "Unexpected web-root path." >&2; exit 4; }
for protected in freecloud_meta freecloud_files freecloud_sessions; do
    [[ -d "$WEB_ROOT/$protected" && ! -L "$WEB_ROOT/$protected" ]] || { echo "Missing/unsafe protected directory: $protected" >&2; exit 4; }
done
[[ -f "$WEB_ROOT/config.json" && ! -L "$WEB_ROOT/config.json" ]] || { echo "Missing/unsafe config.json." >&2; exit 4; }
[[ -f "$WEB_ROOT/freecloud_meta/catalog.lock" ]] || { echo "Missing catalog lock; refusing inconsistent metadata backup." >&2; exit 4; }
[[ ! -e "$BACKUP_ROOT" ]] || { echo "Backup already exists: $BACKUP_ROOT" >&2; exit 4; }

# Hold the application catalog lock throughout its backup and code switch.
exec 9>>"$WEB_ROOT/freecloud_meta/catalog.lock"
flock -x 9

mkdir -p "$(dirname -- "$BACKUP_ROOT")"
install -d -m 0700 "$BACKUP_ROOT" "$BACKUP_ROOT/webroot-php" "$BACKUP_ROOT/pi-client"
cp -a "$WEB_ROOT"/*.php "$BACKUP_ROOT/webroot-php/"
cp -a "$WEB_ROOT/config.json" "$BACKUP_ROOT/"
cp -a "$WEB_ROOT/freecloud_meta" "$BACKUP_ROOT/"
cp -a "$PI_ROOT/freecloud_cli.py" "$BACKUP_ROOT/pi-client/"

# Verify both contents and the exact source inventory before deploying.
for source in "$WEB_ROOT"/*.php; do
    cmp -s "$source" "$BACKUP_ROOT/webroot-php/$(basename -- "$source")" || { echo "PHP backup verification failed: $source" >&2; exit 5; }
done
cmp -s "$WEB_ROOT/config.json" "$BACKUP_ROOT/config.json" || { echo "Config backup verification failed." >&2; exit 5; }
diff -qr "$WEB_ROOT/freecloud_meta" "$BACKUP_ROOT/freecloud_meta" >/dev/null || { echo "Runtime metadata backup verification failed." >&2; exit 5; }
cmp -s "$PI_ROOT/freecloud_cli.py" "$BACKUP_ROOT/pi-client/freecloud_cli.py" || { echo "Pi client backup verification failed." >&2; exit 5; }
find "$BACKUP_ROOT/freecloud_meta" -type f -printf '%P\n' | sort > "$BACKUP_ROOT/metadata-files.txt"
chmod 0600 "$BACKUP_ROOT/metadata-files.txt"
echo "Verified backup: $BACKUP_ROOT"

php -l "$STAGE/freecloud_api.php" >/dev/null
for file in "${PHP_FILES[@]}"; do php -l "$STAGE/$file" >/dev/null; done
python3 - "$STAGE/freecloud_cli.py" <<'PY'
import ast, pathlib, sys
ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
PY

rollback() {
    result=$?
    if [[ $result -ne 0 && -d "$BACKUP_ROOT/webroot-php" ]]; then
        echo "Deployment failed; restoring PHP and Pi client from $BACKUP_ROOT" >&2
        cp -a "$BACKUP_ROOT/webroot-php/." "$WEB_ROOT/"
        cp -a "$BACKUP_ROOT/pi-client/freecloud_cli.py" "$PI_ROOT/freecloud_cli.py"
        uid="$(id -u kbpi)"
        if [[ -S "/run/user/$uid/bus" ]]; then
            runuser -u kbpi -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user restart freecloud-node.service || true
        fi
    fi
    exit "$result"
}
trap rollback EXIT

install_preserving() {
    src=$1 dest=$2 ref=$3
    mode="$(stat -c '%a' "$ref")"
    owner="$(stat -c '%U' "$ref")"
    group="$(stat -c '%G' "$ref")"
    temp="${dest}.fc-deploy-${STAMP}.tmp"
    install -o "$owner" -g "$group" -m "$mode" "$src" "$temp"
    mv -f -- "$temp" "$dest"
}

# Add the shared lifecycle helper first, then dependents; activate the API last.
install_preserving "$STAGE/freecloud_lifecycle.php" "$WEB_ROOT/freecloud_lifecycle.php" "$WEB_ROOT/freecloud_api.php"
for file in freecloud_transfer.php freecloud_download.php freecloud_preview.php freecloud.php; do
    install_preserving "$STAGE/$file" "$WEB_ROOT/$file" "$WEB_ROOT/freecloud_api.php"
done
install_preserving "$STAGE/freecloud_cli.py" "$PI_ROOT/freecloud_cli.py" "$BACKUP_ROOT/pi-client/freecloud_cli.py"
install_preserving "$STAGE/freecloud_api.php" "$WEB_ROOT/freecloud_api.php" "$BACKUP_ROOT/webroot-php/freecloud_api.php"

for file in "${PHP_FILES[@]}"; do
    php -l "$WEB_ROOT/$file" >/dev/null
    [[ "$(sha256sum "$WEB_ROOT/$file" | awk '{print $1}')" == "${EXPECTED[$file]}" ]] || { echo "Installed checksum mismatch: $file" >&2; exit 6; }
done
python3 - "$PI_ROOT/freecloud_cli.py" <<'PY'
import ast, pathlib, sys
ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
PY

uid="$(id -u kbpi)"
[[ -S "/run/user/$uid/bus" ]] || { echo "Pi user service bus unavailable; code is installed but daemon was not restarted." >&2; exit 7; }
runuser -u kbpi -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user restart freecloud-node.service
runuser -u kbpi -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user is-active --quiet freecloud-node.service
echo "Deployment files verified; Pi user daemon restarted. Backup: $BACKUP_ROOT"
trap - EXIT
