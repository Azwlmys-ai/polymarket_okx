"""
main.py — CLI entry point for the Polymarket × OKX research system.

Phase-1 commands:
  init-db   Create / migrate the SQLite schema.
  scan      Collect market data snapshots (bounded run).
  lag       Offline lag recording from collected snapshots.
  report    Lag distribution report (statistics only; reads lag_records).
  paper     Local paper-trading simulation (reads lag_records; no real trading).
  evaluate  Profitability evaluation report from paper_trades (simulated metrics only).
  status    Show a read-only summary of the local SQLite DB contents.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from src.config import get_settings
from src.db import init_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-okx-research",
        description=(
            "Phase-1 research system: data collection, lag recording, and paper trading only."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create the local SQLite schema.")

    scan_p = subparsers.add_parser(
        "scan",
        help="Collect market data snapshots (bounded run).",
    )
    scan_p.add_argument(
        "--source",
        choices=["okx", "polymarket", "all"],
        default="okx",
        help="Data source to collect from (default: okx).",
    )
    scan_p.add_argument(
        "--duration",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Stop collecting after this many seconds (default: 30).",
    )
    scan_p.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Stop after collecting N snapshots total (optional, combines with --duration).",
    )

    lag_p = subparsers.add_parser(
        "lag",
        help="Offline lag recording: detect OKX moves, find Polymarket responses, write lag_records.",
    )
    lag_p.add_argument(
        "--threshold",
        type=float,
        default=0.005,
        metavar="PCT",
        help="Minimum fractional price move to trigger lag search (default: 0.005 = 0.5%%).",
    )
    lag_p.add_argument(
        "--max-lag-ms",
        type=int,
        default=60_000,
        metavar="MS",
        help="Maximum milliseconds to look ahead for a Polymarket response (default: 60000).",
    )

    paper_p = subparsers.add_parser(
        "paper",
        help="Run local paper-trading simulation (reads lag_records, writes paper_trades).",
    )
    paper_p.add_argument(
        "--cash",
        type=float,
        default=100.0,
        metavar="USDC",
        help="Initial simulated cash in USDC (default: 100.0).",
    )
    paper_p.add_argument(
        "--risk",
        type=float,
        default=0.02,
        metavar="PCT",
        help="Max risk fraction per trade (default: 0.02 = 2%%).",
    )
    paper_p.add_argument(
        "--slippage",
        type=float,
        default=0.002,
        metavar="PCT",
        help="Simulated entry slippage fraction (default: 0.002 = 0.2%%).",
    )
    paper_p.add_argument(
        "--fee",
        type=float,
        default=0.001,
        metavar="PCT",
        help="Simulated fee fraction of notional (default: 0.001 = 0.1%%).",
    )
    paper_p.add_argument(
        "--hold-ms",
        type=int,
        default=300_000,
        metavar="MS",
        help="Hold window in milliseconds before seeking exit price (default: 300000 = 5 min).",
    )

    report_p = subparsers.add_parser(
        "report",
        help="Print lag distribution report from local SQLite data.",
    )
    report_p.add_argument(
        "--output",
        nargs="*",
        choices=["json", "markdown"],
        metavar="FORMAT",
        help="Also write report file(s): json and/or markdown (written to reports/).",
    )

    eval_p = subparsers.add_parser(
        "evaluate",
        help=(
            "Evaluate paper-trading simulation results from local SQLite data "
            "(simulated metrics only — not a trading recommendation)."
        ),
    )
    eval_p.add_argument(
        "--output",
        nargs="*",
        choices=["json", "markdown"],
        metavar="FORMAT",
        help="Also write evaluation file(s): json and/or markdown (written to reports/).",
    )

    subparsers.add_parser(
        "status",
        help="Show a read-only summary of the local SQLite DB (snapshot/lag/paper trade counts).",
    )

    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def main() -> int:
    settings = get_settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger(__name__)

    parser = build_parser()
    args = parser.parse_args()

    # ------------------------------------------------------------------ init-db
    if args.command == "init-db":
        db_path = init_db(settings.sqlite_path)
        print(f"Initialized SQLite database at {db_path}")
        return 0

    # ------------------------------------------------------------------ scan
    if args.command == "scan":
        from src.snapshot_store import SnapshotStore, ensure_schema

        db_path = settings.sqlite_path
        ensure_schema(db_path)

        source: str = args.source
        duration: float = args.duration
        count: int | None = args.count

        log.info(
            "Starting scan: source=%s duration=%.0fs max_count=%s db=%s",
            source,
            duration,
            count,
            db_path,
        )

        async def _run() -> int:
            stored = 0

            async with SnapshotStore(db_path) as store:
                if source in ("okx", "all"):
                    from src.okx_ws import OkxWsCollector

                    log.info("OKX collector: symbols=%s", settings.symbols)
                    async with OkxWsCollector(
                        settings.okx_ws_url, settings.symbols
                    ) as collector:
                        async for snapshot in collector.stream(
                            duration_s=duration,
                            max_count=count,
                        ):
                            await store.insert(snapshot)
                            stored += 1
                            log.info(
                                "[%d] OKX %s  last=%.4f  bid=%.4f  ask=%.4f",
                                stored,
                                snapshot.symbol,
                                snapshot.last or 0,
                                snapshot.bid or 0,
                                snapshot.ask or 0,
                            )

                if source in ("polymarket", "all"):
                    from src.polymarket_client import PolymarketCollector

                    remaining_count = (
                        (count - stored) if count is not None else None
                    )
                    log.info(
                        "Polymarket collector: keywords=%s", settings.crypto_keywords
                    )
                    async with PolymarketCollector(
                        settings.polymarket_gamma_url,
                        settings.polymarket_clob_url,
                        settings.crypto_keywords,
                    ) as collector:
                        async for snapshot in collector.stream(
                            duration_s=duration,
                            max_count=remaining_count,
                        ):
                            await store.insert(snapshot)
                            stored += 1
                            log.info(
                                "[%d] POLY %s  last=%s  bid=%s  ask=%s",
                                stored,
                                (snapshot.symbol or "")[:60],
                                f"{snapshot.last:.4f}" if snapshot.last else "—",
                                f"{snapshot.bid:.4f}" if snapshot.bid else "—",
                                f"{snapshot.ask:.4f}" if snapshot.ask else "—",
                            )

            total = 0
            async with SnapshotStore(db_path) as store:
                total = await store.count()

            log.info("Scan complete. Stored %d new snapshots. DB total: %d", stored, total)
            print(f"Scan complete. Stored {stored} snapshots. DB total: {total}")
            return 0

        return asyncio.run(_run())

    # ------------------------------------------------------------------ lag
    if args.command == "lag":
        from src.lag_recorder import run_lag_recording
        from src.snapshot_store import ensure_schema

        db_path = settings.sqlite_path
        ensure_schema(db_path)

        threshold: float = args.threshold
        max_lag_ms: int = args.max_lag_ms

        log.info(
            "Starting lag recording: db=%s threshold=%.3f%% max_lag_ms=%d",
            db_path,
            threshold * 100,
            max_lag_ms,
        )
        inserted = run_lag_recording(db_path, threshold_pct=threshold, max_lag_ms=max_lag_ms)
        print(f"Lag recording complete. {inserted} lag record(s) written to {db_path}")
        return 0

    # ------------------------------------------------------------------ report
    if args.command == "report":
        from src.report import format_report, run_report
        from src.snapshot_store import ensure_schema

        db_path = settings.sqlite_path
        ensure_schema(db_path)

        output_formats: list[str] = args.output or []
        project_root = __import__("pathlib").Path(__file__).resolve().parents[1]
        reports_dir = project_root / "reports"

        log.info("Generating lag report from %s", db_path)
        lag_report = run_report(
            db_path,
            reports_dir=reports_dir if output_formats else None,
            output_formats=output_formats if output_formats else None,
        )
        print(format_report(lag_report))

        if output_formats:
            print(f"\nReport file(s) written to: {reports_dir}")
        return 0

    # ------------------------------------------------------------------ paper
    if args.command == "paper":
        from src.paper_trader import SimConfig, format_summary, run_paper_trading
        from src.snapshot_store import ensure_schema

        db_path = settings.sqlite_path
        ensure_schema(db_path)

        try:
            cfg = SimConfig(
                initial_cash=args.cash,
                max_risk_pct=args.risk,
                slippage_pct=args.slippage,
                fee_pct=args.fee,
                hold_window_ms=args.hold_ms,
            )
        except ValueError as exc:
            print(f"Error: invalid simulation parameter — {exc}")
            return 2

        log.info(
            "Starting paper simulation: cash=%.2f risk=%.1f%% slippage=%.2f%% "
            "fee=%.2f%% hold_ms=%d db=%s",
            cfg.initial_cash,
            cfg.max_risk_pct * 100,
            cfg.slippage_pct * 100,
            cfg.fee_pct * 100,
            cfg.hold_window_ms,
            db_path,
        )

        trades, final_cash = run_paper_trading(db_path, cfg)
        print(format_summary(trades, final_cash, cfg.initial_cash))
        return 0

    # ------------------------------------------------------------------ evaluate
    if args.command == "evaluate":
        from src.evaluator import format_eval_report, run_evaluation
        from src.snapshot_store import ensure_schema

        db_path = settings.sqlite_path
        ensure_schema(db_path)

        output_formats: list[str] = args.output or []
        project_root = __import__("pathlib").Path(__file__).resolve().parents[1]
        reports_dir = project_root / "reports"

        log.info("Generating paper-trading evaluation from %s", db_path)
        metrics = run_evaluation(
            db_path,
            reports_dir=reports_dir if output_formats else None,
            output_formats=output_formats if output_formats else None,
        )
        print(format_eval_report(metrics))

        if output_formats:
            print(f"\nEvaluation file(s) written to: {reports_dir}")
        return 0

    # ------------------------------------------------------------------ status
    if args.command == "status":
        from src.db_status import format_status, query_status

        db_path = settings.sqlite_path
        status = query_status(db_path)
        print(format_status(status))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
