#!/usr/bin/env bash
# vps_baseline_check.sh — 一键 VPS 运维基线 + 健康检查
# 在 VPS 上执行: bash /opt/polymarket_okx/scripts/vps_baseline_check.sh
# 结果纯文本输出，无副作用，不修改任何文件

set -uo pipefail
SEP="════════════════════════════════════════"

echo "$SEP"
echo "  VPS BASELINE + HEALTH CHECK"
echo "  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "$SEP"

echo ""
echo "── [1] SYSTEM ──────────────────────────"
echo "hostname    : $(hostname)"
echo "uptime      : $(uptime -p 2>/dev/null || uptime)"
echo "kernel      : $(uname -r)"
echo "os          : $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '"')"

echo ""
echo "── [2] RESOURCES ───────────────────────"
echo "--- CPU ---"
nproc
grep "model name" /proc/cpuinfo | head -1
echo "--- Load ---"
cat /proc/loadavg
echo "--- Memory (MB) ---"
free -m | grep -E "Mem|Swap"
echo "--- Disk ---"
df -h / /opt 2>/dev/null | head -5

echo ""
echo "── [3] PYTHON ──────────────────────────"
python3 --version 2>&1
which python3
ls /opt/polymarket_okx/.venv/bin/python3 2>/dev/null && echo "venv OK" || echo "venv NOT FOUND"

echo ""
echo "── [4] PROJECT DIRECTORY ───────────────"
ls -la /opt/polymarket_okx/ 2>/dev/null || echo "ERROR: /opt/polymarket_okx not found"
echo "--- src/ ---"
ls /opt/polymarket_okx/src/ 2>/dev/null
echo "--- scripts/ ---"
ls /opt/polymarket_okx/scripts/ 2>/dev/null
echo "--- research/ (latest 10) ---"
ls -lt /opt/polymarket_okx/research/ 2>/dev/null | head -12

echo ""
echo "── [5] SSH SECURITY ────────────────────"
echo "--- authorized_keys ---"
wc -l ~/.ssh/authorized_keys 2>/dev/null && echo "keys count: $(grep -c 'ssh-' ~/.ssh/authorized_keys 2>/dev/null || echo 0)"
echo "--- sshd PasswordAuthentication ---"
grep -E "^PasswordAuthentication|^#PasswordAuthentication" /etc/ssh/sshd_config 2>/dev/null || echo "not found in sshd_config"
grep -rE "^PasswordAuthentication" /etc/ssh/sshd_config.d/ 2>/dev/null || echo "(no override in sshd_config.d)"
echo "--- PubkeyAuthentication ---"
grep -E "^PubkeyAuthentication|^#PubkeyAuthentication" /etc/ssh/sshd_config 2>/dev/null || echo "not found"

echo ""
echo "── [6] SYSTEMD SERVICES ────────────────"
systemctl list-units --type=service --state=running | grep -E "polymarket|okx|anchor" || echo "no matching running services"
echo "--- polymarket-okx-anchor.service status ---"
systemctl status polymarket-okx-anchor.service --no-pager -l 2>/dev/null | head -30 || echo "service not found"
echo "--- polymarket-health-check.timer ---"
systemctl status polymarket-health-check.timer --no-pager 2>/dev/null | head -10 || echo "timer not found"

echo ""
echo "── [7] TMUX SESSIONS ───────────────────"
tmux ls 2>/dev/null || echo "no tmux sessions or tmux not running"

echo ""
echo "── [8] JOURNAL (anchor service last 30 lines) ──"
journalctl -u polymarket-okx-anchor.service -n 30 --no-pager 2>/dev/null || echo "journal not available"

echo ""
echo "── [9] EVENT FILES ─────────────────────"
echo "--- paper_anchor_signal_events.jsonl ---"
PAPER=/opt/polymarket_okx/research/paper_anchor_signal_events.jsonl
if [ -f "$PAPER" ]; then
    echo "size: $(wc -l < $PAPER) lines, $(du -sh $PAPER | cut -f1)"
    echo "last 3 lines:"
    tail -3 "$PAPER"
else
    echo "FILE NOT FOUND: $PAPER"
    find /opt/polymarket_okx -name "*paper*signal*" 2>/dev/null | head -5
fi

echo ""
echo "--- shadow_execution_events.jsonl ---"
SHADOW=/opt/polymarket_okx/research/shadow_execution_events.jsonl
if [ -f "$SHADOW" ]; then
    echo "size: $(wc -l < $SHADOW) lines, $(du -sh $SHADOW | cut -f1)"
    echo "last 3 lines:"
    tail -3 "$SHADOW"
else
    echo "FILE NOT FOUND: $SHADOW"
    find /opt/polymarket_okx -name "*shadow*execution*" 2>/dev/null | head -5
fi

echo ""
echo "── [10] FALLBACK RATIO ─────────────────"
PAPER=/opt/polymarket_okx/research/paper_anchor_signal_events.jsonl
if [ -f "$PAPER" ]; then
    TOTAL=$(wc -l < "$PAPER")
    FALLBACK=$(grep -c '"fallback"' "$PAPER" 2>/dev/null || echo 0)
    echo "total events : $TOTAL"
    echo "fallback hits: $FALLBACK"
    if [ "$TOTAL" -gt 0 ]; then
        python3 -c "print(f'fallback ratio: {$FALLBACK/$TOTAL*100:.1f}%')" 2>/dev/null || echo "ratio calc skipped"
    fi
else
    echo "paper event file not found, skip fallback ratio"
fi

echo ""
echo "── [11] RECENT ERRORS (last 50 journal lines) ──"
journalctl -u polymarket-okx-anchor.service -n 50 --no-pager 2>/dev/null | grep -iE "error|exception|traceback|critical|fatal" | tail -20 || echo "no errors found or journal unavailable"

echo ""
echo "── [12] VPS HEALTH REPORT (latest) ────"
REPORT=/opt/polymarket_okx/research/vps_health_report.md
if [ -f "$REPORT" ]; then
    echo "modified: $(stat -c '%y' $REPORT 2>/dev/null | cut -d. -f1)"
    cat "$REPORT"
else
    echo "vps_health_report.md not found"
fi

echo ""
echo "$SEP"
echo "  BASELINE CHECK COMPLETE"
echo "  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "$SEP"
