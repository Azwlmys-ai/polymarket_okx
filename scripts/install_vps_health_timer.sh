#!/usr/bin/env bash
# install_vps_health_timer.sh
# Run ON THE VPS (sudo). Installs a systemd timer that runs vps_health_check.py every 5 minutes.
#
# Usage (on VPS as root/sudo):
#   cd /opt/polymarket_okx && sudo bash scripts/install_vps_health_timer.sh
#
# What it does:
#   1. Creates /etc/systemd/system/polymarket-health-check.service (oneshot)
#   2. Creates /etc/systemd/system/polymarket-health-check.timer (every 5 min)
#   3. Enables and starts the timer
#
# Output (on VPS):
#   /opt/polymarket_okx/research/vps_health_report.md
#   /opt/polymarket_okx/research/vps_health_events.jsonl
#
# To check status:
#   systemctl status polymarket-health-check.timer
#   systemctl list-timers polymarket-health-check.timer
#   cat /opt/polymarket_okx/research/vps_health_report.md

set -euo pipefail

SERVICE_NAME="polymarket-health-check"
VPS_ROOT="/opt/polymarket_okx"
PYTHON_BIN="${PYTHON_BIN:-python3}"
WORKDIR="${VPS_ROOT}"

echo "==> Creating systemd service: ${SERVICE_NAME}.service"

cat <<EOF | sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null
[Unit]
Description=Polymarket OKX VPS Health Check (read-only)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=${WORKDIR}
ExecStart=${PYTHON_BIN} ${WORKDIR}/research/vps_health_check.py
StandardOutput=journal
StandardError=journal
Environment="VPS_ROOT=${VPS_ROOT}"
Environment="HEALTH_LOOKBACK_SHADOW=100"
Environment="HEALTH_RECENT_MINUTES=5"
Nice=19
IOSchedulingClass=idle
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=${VPS_ROOT}/research
PrivateTmp=yes

# Run every 5 min via timer, don't restart on failure
EOF

echo "==> Creating systemd timer: ${SERVICE_NAME}.timer"

cat <<EOF | sudo tee /etc/systemd/system/${SERVICE_NAME}.timer > /dev/null
[Unit]
Description=Polymarket OKX VPS Health Check Timer (every 5 min)
Requires=${SERVICE_NAME}.service

[Timer]
OnBootSec=60
OnUnitActiveSec=300
Persistent=true
AccuracySec=10
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

echo "==> Reloading systemd..."
sudo systemctl daemon-reload

echo "==> Enabling and starting timer..."
sudo systemctl enable "${SERVICE_NAME}.timer"
sudo systemctl start "${SERVICE_NAME}.timer"

echo "==> Triggering first run..."
sudo systemctl start "${SERVICE_NAME}.service" || echo "First run failed (may need service file fixes)"

echo ""
echo "✅ Install complete."
echo ""
echo "To check timer status:"
echo "  systemctl status ${SERVICE_NAME}.timer"
echo "  systemctl list-timers ${SERVICE_NAME}.timer"
echo ""
echo "To view latest report:"
echo "  cat ${VPS_ROOT}/research/vps_health_report.md"
echo ""
echo "To view event history:"
echo "  tail -5 ${VPS_ROOT}/research/vps_health_events.jsonl"
echo ""
echo "To stop:"
echo "  sudo systemctl disable --now ${SERVICE_NAME}.timer"