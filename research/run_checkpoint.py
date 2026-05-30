"""
run_checkpoint.py — Shadow accumulation checkpoint（本地运行，只读）

Usage:
    python3 research/run_checkpoint.py

功能：
  1. SSH 到 VPS 采集所有只读指标
  2. 计算当前 real-CLOB attribution 样本数
  3. 若 >= 300 → 自动 scp 最新文件，回填 outcomes，跑 attribution，输出完整报告
  4. 若 < 300 → 输出当前进度，告知还差多少件

禁止：不修改 VPS 任何文件，不重启服务，不开实盘。
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────
VPS = "root@158.247.220.86"
PROJ = "/opt/polymarket_okx"
LOCAL_RESEARCH = Path(__file__).parent          # ~/polymarket_okx/research/
THRESHOLD_GO = 300                               # G2 gate
TAKER_FEE = 0.07

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def ssh(cmd: str, timeout: int = 20) -> tuple[str, int]:
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new", VPS, cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return (result.stdout + result.stderr).strip(), result.returncode


def scp(remote: str, local: Path, timeout: int = 60) -> bool:
    result = subprocess.run(
        ["scp", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
         f"{VPS}:{remote}", str(local)],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode == 0


def load_jsonl(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def fee_adj_pnl(entry: float, payout: float) -> float:
    return payout - entry - TAKER_FEE * (1.0 - entry)


def breakeven_fill(win_rate: float) -> float:
    for fp_int in range(1, 1000):
        fp = fp_int / 1000
        ev = win_rate * fee_adj_pnl(fp, 1.0) + (1 - win_rate) * fee_adj_pnl(fp, 0.0)
        if ev < 0:
            return (fp_int - 1) / 1000
    return 1.0


# ── Step 1: VPS 指标采集 ───────────────────────────────────────────────────────

def collect_vps_metrics() -> dict:
    print("▶ 连接 VPS 采集指标...")

    # 主服务状态
    svc_out, _ = ssh(
        "systemctl is-active polymarket-okx-anchor.service 2>/dev/null"
    )
    service_active = svc_out.strip() == "active"

    # signal events 行数
    sig_count_out, _ = ssh(
        f"wc -l < {PROJ}/research/paper_anchor_signal_events.jsonl 2>/dev/null || echo 0"
    )
    try:
        sig_count = int(sig_count_out.strip().split()[0])
    except (ValueError, IndexError):
        sig_count = 0

    # shadow events 行数 + 最后写入时间
    shadow_info_out, _ = ssh(
        f"wc -l < {PROJ}/research/shadow_execution_events.jsonl 2>/dev/null || echo 0"
    )
    try:
        shadow_count = int(shadow_info_out.strip().split()[0])
    except (ValueError, IndexError):
        shadow_count = 0

    shadow_mtime_out, _ = ssh(
        f"stat -c %Y {PROJ}/research/shadow_execution_events.jsonl 2>/dev/null || echo 0"
    )
    try:
        shadow_mtime = int(shadow_mtime_out.strip())
        shadow_age_s = int(datetime.now(timezone.utc).timestamp()) - shadow_mtime
    except (ValueError, TypeError):
        shadow_age_s = -1

    # shadow 统计（最近 200 条）
    shadow_stats_script = r"""
python3 -c "
import json, sys
from pathlib import Path
from collections import Counter

p = Path('/opt/polymarket_okx/research/shadow_execution_events.jsonl')
lines = p.read_text().splitlines()[-200:]
evts = []
for l in lines:
    l = l.strip()
    if l:
        try: evts.append(json.loads(l))
        except: pass

n = len(evts)
fallback = sum(1 for e in evts if e.get('fallback_used'))
clob_ok  = sum(1 for e in evts if e.get('clob_orderbook_available'))
exec10   = sum(1 for e in evts if e.get('executable_10'))
exec25   = sum(1 for e in evts if e.get('executable_25'))
exec50   = sum(1 for e in evts if e.get('executable_50'))
with_fill = [e for e in evts if not e.get('fallback_used') and e.get('estimated_fill_price_10') is not None]
latencies = [e['clob_fetch_latency_ms'] for e in evts if e.get('clob_fetch_latency_ms')]
latencies.sort()
p50 = latencies[len(latencies)//2] if latencies else 0
p95 = latencies[int(len(latencies)*0.95)] if latencies else 0

print(f'n={n}')
print(f'fallback_rate={fallback/n*100:.1f}')
print(f'clob_avail_rate={clob_ok/n*100:.1f}')
print(f'exec10={exec10/n*100:.1f}')
print(f'exec25={exec25/n*100:.1f}')
print(f'exec50={exec50/n*100:.1f}')
print(f'real_clob_with_fill={len(with_fill)}')
print(f'lat_p50={p50:.0f}')
print(f'lat_p95={p95:.0f}')
" 2>/dev/null
"""
    stats_out, _ = ssh(shadow_stats_script.strip())
    parsed = {}
    for line in stats_out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k.strip()] = v.strip()

    # tmux session 存活
    tmux_out, _ = ssh("tmux ls 2>/dev/null | grep shadow_follow || echo 'MISSING'")
    tmux_alive = "MISSING" not in tmux_out

    # journal 最近 5 分钟错误数
    journal_out, _ = ssh(
        "journalctl -u polymarket-okx-anchor.service --since '5 min ago' "
        "--no-pager -q 2>/dev/null | grep -ci 'error\\|exception\\|traceback' || echo 0"
    )
    try:
        recent_errors = int(journal_out.strip())
    except ValueError:
        recent_errors = 0

    # 全量 real-CLOB 样本数（不含 fallback）
    total_realclob_out, _ = ssh(
        r"""python3 -c "
import json
from pathlib import Path
p = Path('/opt/polymarket_okx/research/shadow_execution_events.jsonl')
n = sum(1 for l in p.read_text().splitlines() if l.strip() and not json.loads(l).get('fallback_used'))
print(n)
" 2>/dev/null"""
    )
    try:
        total_real_clob = int(total_realclob_out.strip())
    except ValueError:
        total_real_clob = 0

    return {
        "service_active": service_active,
        "sig_count": sig_count,
        "shadow_count": shadow_count,
        "shadow_age_s": shadow_age_s,
        "tmux_alive": tmux_alive,
        "recent_errors": recent_errors,
        "total_real_clob": total_real_clob,
        **parsed,
    }


# ── Step 2: 打印 checkpoint 报告 ───────────────────────────────────────────────

def print_checkpoint(m: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print()
    print("=" * 60)
    print(f"  Shadow Accumulation Checkpoint — {ts}")
    print("=" * 60)

    def ok(cond): return "✅" if cond else "❌"

    print(f"\n── 主服务 ──────────────────────────────────────")
    print(f"  systemd active      : {ok(m['service_active'])} {m['service_active']}")
    print(f"  tmux shadow_follow  : {ok(m['tmux_alive'])}")
    print(f"  journal errors(5m)  : {m['recent_errors']}")

    print(f"\n── 信号与 shadow 事件 ──────────────────────────")
    print(f"  signal events 总行数 : {m['sig_count']}")
    print(f"  shadow events 总行数 : {m['shadow_count']}")
    age = m["shadow_age_s"]
    age_str = f"{age}s 前" if age >= 0 else "未知"
    print(f"  shadow 文件最后写入  : {age_str}")

    print(f"\n── 最近 200 条 shadow 统计 ─────────────────────")
    print(f"  fallback 比例        : {m.get('fallback_rate', '?')}%  {ok(float(m.get('fallback_rate',100)) < 20)}")
    print(f"  CLOB available 率    : {m.get('clob_avail_rate', '?')}%")
    print(f"  executable @$10/25/50: {m.get('exec10','?')}% / {m.get('exec25','?')}% / {m.get('exec50','?')}%")
    print(f"  book latency p50/p95 : {m.get('lat_p50','?')}ms / {m.get('lat_p95','?')}ms")

    print(f"\n── Attribution 进度 ────────────────────────────")
    n = m["total_real_clob"]
    remaining = max(0, THRESHOLD_GO - n)
    pct = min(100, n / THRESHOLD_GO * 100)
    bar_len = 30
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"  real-CLOB 样本数     : {n} / {THRESHOLD_GO}  [{bar}] {pct:.0f}%")
    if remaining > 0:
        print(f"  还差                 : {remaining} 件")
    else:
        print(f"  🟢 已达 {THRESHOLD_GO} 件，准备跑 attribution！")
    print()


# ── Step 3: 达到 300 件时跑 full attribution ──────────────────────────────────

def run_full_attribution() -> None:
    print("▶ 达到 300 件，开始拉取 VPS 最新文件...")
    today = datetime.now().strftime("%Y%m%d")

    shadow_local = LOCAL_RESEARCH / f"shadow_execution_events_vps_{today}.jsonl"
    signals_local = LOCAL_RESEARCH / f"paper_anchor_signals_vps_{today}.jsonl"
    backfilled = LOCAL_RESEARCH / f"shadow_events_backfilled_{today}.jsonl"
    result_path = LOCAL_RESEARCH / "shadow_attribution_result.md"
    prev_result = LOCAL_RESEARCH / "shadow_attribution_result_prev.md"

    # 备份上次结果
    if result_path.exists():
        import shutil
        shutil.copy(result_path, prev_result)
        print(f"  旧报告备份至 {prev_result.name}")

    # scp shadow events
    print(f"  scp shadow_execution_events.jsonl → {shadow_local.name}")
    if not scp(f"{PROJ}/research/shadow_execution_events.jsonl", shadow_local):
        print("  ❌ scp shadow events 失败，跳过 attribution")
        return

    # scp signals
    print(f"  scp paper_anchor_signals.jsonl → {signals_local.name}")
    if not scp(f"{PROJ}/research/paper_anchor_signals.jsonl", signals_local):
        print("  ❌ scp signals 失败，跳过 attribution")
        return

    print("  ▶ 回填 outcomes...")
    ret = subprocess.run(
        [sys.executable, str(LOCAL_RESEARCH / "backfill_shadow_outcomes.py"),
         str(shadow_local), str(signals_local), str(backfilled)],
        capture_output=True, text=True
    )
    print(ret.stdout[-800:] if ret.stdout else "（无输出）")
    if ret.returncode != 0:
        print(f"  ❌ backfill 失败：{ret.stderr[:300]}")
        return

    print("  ▶ 跑 attribution...")
    ret2 = subprocess.run(
        [sys.executable, str(LOCAL_RESEARCH / "run_shadow_attribution.py"),
         str(backfilled)],
        capture_output=True, text=True
    )
    print(ret2.stdout)
    if ret2.returncode != 0:
        print(f"  ❌ attribution 失败：{ret2.stderr[:300]}")
        return

    # 对比上次结果，输出 diff 摘要
    if prev_result.exists():
        print("\n── 与上次报告对比 ──────────────────────────────")
        def extract_metric(text: str, keyword: str) -> str:
            for line in text.splitlines():
                if keyword in line:
                    return line.strip()
            return ""
        prev = prev_result.read_text()
        curr = result_path.read_text()
        for kw in ["Mean shadow_adjusted_pnl_10", "Shadow win rate",
                   "Mean fill price", "Gates passed"]:
            p_line = extract_metric(prev, kw)
            c_line = extract_metric(curr, kw)
            if p_line or c_line:
                print(f"  旧: {p_line}")
                print(f"  新: {c_line}")
                print()

    print(f"\n✅ Attribution 报告已更新：research/shadow_attribution_result.md")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        metrics = collect_vps_metrics()
    except subprocess.TimeoutExpired:
        print("❌ SSH 连接超时，请检查网络或 VPS 状态。")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 采集失败：{e}")
        sys.exit(1)

    print_checkpoint(metrics)

    n = metrics["total_real_clob"]
    if n >= THRESHOLD_GO:
        run_full_attribution()
    else:
        print(f"当前 {n} 件，继续等待。下次 checkpoint 再运行本脚本。")
        print(f"参考节奏：VPS 每天约产生 60–80 件 real-CLOB 事件，")
        print(f"预计还需 {max(1, (THRESHOLD_GO - n) // 70)} 天左右达到 300 件。")


if __name__ == "__main__":
    main()
