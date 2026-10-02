#!/usr/bin/env bash
# Install the 945 lifecycle on the PSYGRID VM WITHOUT touching the PSYGRID service.
# Usage (as root, from the repository checkout):  bash deploy/systemd/install.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
id psygrid945 >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin psygrid945
mkdir -p /opt/psygrid-945 /var/lib/psygrid-945
rsync -a --delete --exclude '.git' --exclude 'data' --exclude '__pycache__' "$SRC"/ /opt/psygrid-945/
chown -R root:root /opt/psygrid-945
chown -R psygrid945:psygrid945 /var/lib/psygrid-945
install -m 0644 "$SRC/deploy/systemd/psygrid-945.service" /etc/systemd/system/psygrid-945.service
install -m 0644 "$SRC/deploy/systemd/psygrid-945.timer" /etc/systemd/system/psygrid-945.timer
cd /opt/psygrid-945 && sudo -u psygrid945 python3 945.py --self-test >/dev/null && echo "self-test OK"
systemctl daemon-reload
systemctl enable --now psygrid-945.timer
echo; echo "== verification =="
systemctl list-timers psygrid-945.timer --no-pager
systemctl is-active psygrid-945.timer
ss -ltnp | grep -c python3 | xargs -I{} echo "python listening sockets (945 must add none): {}"
sudo -u psygrid945 python3 /opt/psygrid-945/945.py --status --data-dir /var/lib/psygrid-945 || true
