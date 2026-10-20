#!/usr/bin/env bash
# Install switch_backup on a systemd-based Linux box (Debian/Ubuntu/RHEL family).
# Run from the project folder:  sudo bash deploy/install.sh
# Needs python3 with the venv module (Debian/Ubuntu: apt install python3-venv).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run this as root: sudo bash $0" >&2; exit 1; }

APP=/opt/switch-backup
OUT=/var/backups/switches
SVC=switchbackup
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

id -u "$SVC" &>/dev/null || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin "$SVC"

install -d -o root -g "$SVC" -m 750 "$APP"
install -m 640 -o root -g "$SVC" "$HERE/switch_backup.py" "$HERE/requirements.txt" "$APP/"

# Keep an existing inventory / logins on re-install; never overwrite them
[[ -f "$APP/inventory.csv" ]] || install -m 640 -o root -g "$SVC" "$HERE/inventory.csv" "$APP/inventory.csv"
if [[ ! -f "$APP/.env" ]]; then
    src="$HERE/.env"; [[ -f "$src" ]] || src="$HERE/.env.example"
    install -m 600 -o "$SVC" -g "$SVC" "$src" "$APP/.env"
fi

python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"
chown -R root:"$SVC" "$APP/venv"

install -d -o "$SVC" -g "$SVC" -m 700 "$OUT"
install -m 644 "$HERE/deploy/switch-backup.service" "$HERE/deploy/switch-backup.timer" /etc/systemd/system/
systemctl daemon-reload

cat <<MSG

Installed to $APP (backups go to $OUT).
Next:
  1. Put the real logins in $APP/.env   (sudo -u $SVC nano $APP/.env)
  2. Try it on one switch:
       sudo -u $SVC $APP/venv/bin/python $APP/switch_backup.py -o $OUT --only MDF1
  3. Schedule it (nightly 02:00, edit /etc/systemd/system/switch-backup.timer to change):
       sudo systemctl enable --now switch-backup.timer
     Run it now:   sudo systemctl start switch-backup
     Check it:     systemctl status switch-backup   |   journalctl -u switch-backup
MSG
