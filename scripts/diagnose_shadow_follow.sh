#!/usr/bin/env bash
# diagnose_shadow_follow.sh — shadow_follow 断写诊断（只读，无副作用）
# 在本地终端执行：bash ~/polymarket_okx/scripts/diagnose_shadow_follow.sh
#
# 检查内容：
#   A. tmux session 是否存在，进程是否活跃
#   B. tmux 最近输出（判断是 idle/错误/崩溃）
#   C. shadow_execution_events.jsonl 时间线断点
#   D. shadow_execution_recorder.py 进程状态
#   E. 系统时间/日志中的异常

set -uo pipefail
VPS="root@158.247.220.86"
SEP="════════════════════════════════════════"

echo "$SEP"
echo "  SHADOW_FOLLOW 断写诊断  $(date '+%Y-%m-%d %H:%M:%S')"
echo "$SEP"

ssh -o BatchMode=yes "$VPS" bash << 'REMOTE'
set -uo pipefail
SEP="════════════════════════════════════════"
SHADOW_FILE="/opt/polymarket_okx/research/shadow_execution_events.jsonl"
RECORDER="/opt/polymarket_okx/research/shadow_execution_recorder.py"
NOW=$(date +%s)

echo ""
echo "── [A] tmux session 存在性 ──────────────"
tmux ls 2>/dev/null || echo "ERROR: tmux 无 session 或 tmux 未运行"

echo ""
echo "── [B] tmux shadow_follow 最近 60 行输出 ─"
if tmux has-session -t shadow_follow 2>/dev/null; then
    echo "[session exists]"
    # 扩大捕获缓冲区后抓取
    tmux capture-pane -t shadow_follow -p -S -200 2>/dev/null | grep -v '^$' | tail -60 \
        || echo "capture-pane 失败"
else
    echo "SESSION NOT FOUND"
fi

echo ""
echo "── [C] shadow_execution_events.jsonl 时间线 ─"
if [ -f "$SHADOW_FILE" ]; then
    TOTAL=$(wc -l < "$SHADOW_FILE")
    FSIZE=$(du -sh "$SHADOW_FILE" | cut -f1)
    LAST_MTIME=$(stat -c %Y "$SHADOW_FILE" 2>/dev/null)
    STALE_S=$(( NOW - LAST_MTIME ))
    echo "总行数   : $TOTAL"
    echo "文件大小 : $FSIZE"
    echo "距上次写入: ${STALE_S}s 前（$(date -d @$LAST_MTIME '+%Y-%m-%d %H:%M:%S UTC')）"
    echo ""
    echo "--- 最后 5 条事件（ts_utc + reject_reason + fallback_used）---"
    tail -5 "$SHADOW_FILE" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        print(d.get('ts_utc','?'), '|', d.get('reject_reason','?'), '| fallback='+str(d.get('fallback_used','?')), '|', d.get('slug','?'))
    except Exception as e:
        print('PARSE ERROR:', e, line[:80])
"
    echo ""
    echo "--- 事件时间间隔分析（最后 20 条）---"
    tail -20 "$SHADOW_FILE" | python3 -c "
import sys, json
from datetime import datetime, timezone
times = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        ts = d.get('ts_utc')
        if ts:
            times.append(datetime.fromisoformat(ts.replace('Z','+00:00')))
    except: pass
if len(times) >= 2:
    times.sort()
    gaps = [(times[i+1]-times[i]).total_seconds() for i in range(len(times)-1)]
    print(f'最早: {times[0]}')
    print(f'最晚: {times[-1]}')
    print(f'最大间隔: {max(gaps):.0f}s')
    print(f'平均间隔: {sum(gaps)/len(gaps):.0f}s')
    print(f'距现在: {(datetime.now(timezone.utc)-times[-1]).total_seconds():.0f}s')
else:
    print('事件数不足')
"
else
    echo "FILE NOT FOUND: $SHADOW_FILE"
fi

echo ""
echo "── [D] shadow_execution_recorder.py 进程 ─"
ps aux | grep shadow_execution_recorder | grep -v grep || echo "进程未运行"
echo ""
# 查看 /proc 下是否有 python 进程打开了该文件
PROCS=$(lsof "$SHADOW_FILE" 2>/dev/null) && echo "$PROCS" || echo "lsof: 无进程持有该文件句柄（文件已关闭或未运行）"

echo ""
echo "── [E] recorder 进程内存/CPU（若存在）──"
PID=$(pgrep -f shadow_execution_recorder 2>/dev/null | head -1)
if [ -n "${PID:-}" ]; then
    echo "PID: $PID"
    cat /proc/$PID/status 2>/dev/null | grep -E "VmRSS|VmPeak|State|Threads"
    echo "进程运行时长: $(ps -o etimes= -p $PID 2>/dev/null | xargs)s"
else
    echo "无 shadow_execution_recorder 进程"
fi

echo ""
echo "── [F] journal 中 recorder 相关日志（最近 2h）──"
journalctl --since "2 hours ago" --no-pager 2>/dev/null \
    | grep -i "shadow\|recorder\|follow" | tail -20 \
    || echo "journal 无相关日志"

echo ""
echo "── [G] 主服务最新 window 活动（做时间对比）──"
journalctl -u polymarket-okx-anchor.service -n 5 --no-pager 2>/dev/null \
    | grep -E "Spawned|RESOLVED|SIGNAL" | tail -5

echo "$SEP"
echo "  诊断完成  $(date '+%Y-%m-%d %H:%M:%S UTC')"
echo "$SEP"
REMOTE
