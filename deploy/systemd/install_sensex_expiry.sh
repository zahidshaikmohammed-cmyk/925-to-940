#!/usr/bin/env bash
# Install the SENSEX expiry engine + dashboard as a weekday service.
# Usage (as root, from the repository checkout):  bash deploy/systemd/install_sensex_expiry.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
id sensexexp >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin sensexexp
mkdir -p /opt/sensex-expiry /var/lib/sensex-expiry
rsync -a --delete --exclude '.git' --exclude 'data' --exclude 'reports' --exclude '__pycache__' "$SRC"/ /opt/sensex-expiry/
chown -R root:root /opt/sensex-expiry
chown -R sensexexp:sensexexp /var/lib/sensex-expiry
if [ ! -f /etc/sensex-expiry.env ]; then
  umask 077
  printf 'DHAN_CLIENT_ID=\nDHAN_ACCESS_TOKEN=\nSENSEX_MODE=paper\n' > /etc/sensex-expiry.env
  echo "created /etc/sensex-expiry.env -- fill in the Dhan credentials (mode stays 'paper' until validation passes)"
fi
python3 -m pip install --quiet 'dhanhq==2.2.0'
install -m 0644 "$SRC/deploy/systemd/sensex-expiry.service" /etc/systemd/system/sensex-expiry.service
install -m 0644 "$SRC/deploy/systemd/sensex-expiry.timer" /etc/systemd/system/sensex-expiry.timer
cd /opt/sensex-expiry && sudo -u sensexexp python3 -m sensex_expiry --self-test
systemctl daemon-reload
systemctl enable --now sensex-expiry.timer
systemctl list-timers sensex-expiry.timer --no-pager
echo "Dashboard (when running): ssh -N -L 8765:127.0.0.1:8765 <this-vm>  then open http://127.0.0.1:8765"
