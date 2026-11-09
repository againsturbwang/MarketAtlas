"""CLI entry point.

    python run.py init               # create schema + seed the catalog
    python run.py collect [--only id] [--force]
    python run.py serve              # API + dashboard only
    python run.py run                # collector + dashboard (normal mode)
    python run.py status
    python run.py proxy --test
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import BASE_DIR, SETTINGS
from db import Database
from markets import MARKETS, MARKET_MAP

LOG_DIR = BASE_DIR / "logs"


def setup_logging(level: str = "INFO") -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        stream.setLevel(getattr(logging, level, logging.INFO))
        root.addHandler(stream)
    file = RotatingFileHandler(LOG_DIR / "monitor.log", maxBytes=4_000_000, backupCount=3,
                               encoding="utf-8")
    file.setFormatter(fmt)
    file.setLevel(logging.INFO)
    root.addHandler(file)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build(settings=SETTINGS):
    from collector import Collector
    from em_client import EastMoneyClient
    from proxy_pool import ProxyPool
    from intel import IntelHub
    from signal_hub import SignalHub
    from timesync import Clock

    db = Database(settings.db_path)
    db.sync_markets(MARKETS)
    pool = ProxyPool(settings, db=db, logger=logging.getLogger("em.proxy"))
    clock = Clock()
    clock.sync()
    client = EastMoneyClient(settings, pool=pool)
    collector = Collector(settings, db, client, pool=pool, clock=clock)
    hub = SignalHub(settings, db, client, clock=clock, pool=pool)
    intel_hub = IntelHub(settings, db, client, clock=clock)
    return db, pool, client, collector, clock, hub, intel_hub


def cmd_init(args) -> int:
    db, pool, client, collector, clock, hub, intel_hub = build()
    print(f"database : {SETTINGS.db_path}")
    print(f"markets  : {len(MARKETS)} seeded")
    print(f"clock    : {json.dumps(clock.status(), ensure_ascii=False)}")
    print(f"proxy    : {'enabled' if pool.enabled else 'disabled'}"
          f" ({SETTINGS.proxy_apikey or 'no key'})")
    if args.proxy_test and pool.enabled:
        n = pool.refresh(force=True)
        print(f"extracted: {n} proxies -> {json.dumps(pool.status(), ensure_ascii=False)}")
    return 0


def cmd_collect(args) -> int:
    db, pool, client, collector, clock, hub, intel_hub = build()
    if args.proxy_test:
        pool.refresh(force=True)
    only = args.only.split(",") if args.only else None
    if only:
        unknown = [m for m in only if m not in MARKET_MAP]
        if unknown:
            print(f"unknown market(s): {', '.join(unknown)}")
            return 2
    summary = collector.run_once(only=only, force=True)
    print(json.dumps(summary, ensure_ascii=False))
    if args.klines:
        written = collector.refresh_klines(force=True)
        print(f"kline bars written: {written}")
    return 0


def cmd_serve(args) -> int:
    from server import Monitor, serve

    db, pool, client, collector, clock, hub, intel_hub = build()
    monitor = Monitor(SETTINGS, db, collector=collector, pool=pool, client=client,
                      clock=clock, hub=hub, intel_hub=intel_hub)
    clock.start()
    if pool.enabled:
        pool.start()
    monitor.start_refresher()
    serve(monitor, args.host or SETTINGS.host, args.port or SETTINGS.port)
    return 0


def cmd_run(args) -> int:
    from server import Monitor, serve

    db, pool, client, collector, clock, hub, intel_hub = build()
    monitor = Monitor(SETTINGS, db, collector=collector, pool=pool, client=client,
                      clock=clock, hub=hub, intel_hub=intel_hub)
    clock.start()
    if pool.enabled:
        pool.start()
    monitor.start_refresher()
    thread = threading.Thread(target=collector.loop, name="collector", daemon=True)
    thread.start()
    if SETTINGS.signals_enabled:
        hub.start()
        intel_hub.start()
    stop = threading.Event()

    def _stop(*_):
        collector.stop_event.set()
        hub.stop_event.set()
        intel_hub.stop_event.set()
        stop.set()
        threading.Thread(target=lambda: (time.sleep(0.6), sys.exit(0)), daemon=True).start()

    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except ValueError:
        pass
    try:
        serve(monitor, args.host or SETTINGS.host, args.port or SETTINGS.port)
    finally:
        collector.stop_event.set()
        hub.stop_event.set()
    return 0


def cmd_status(args) -> int:
    db = Database(SETTINGS.db_path)
    db.sync_markets(MARKETS)
    counts = db.counts()
    states = db.collect_states()
    stats = db.market_stats_latest()
    now = int(time.time())
    print(f"database : {SETTINGS.db_path}  ({SETTINGS.db_path.stat().st_size / 1e6:.1f} MB)")
    print(f"rows     : {counts['instruments']:,} instruments / {counts['history_points']:,} history points")
    print(f"markets  : {counts['markets']} configured, {len(states)} collected")
    print()
    print(f"{'market':<18}{'rows':>7}{'total':>7}{'avg%':>8}{'up/down':>12}{'age':>7}  state")
    for m in db.markets():
        state = states.get(m["id"], {})
        st = stats.get(m["id"], {})
        age = now - (state.get("last_ts") or 0) if state.get("last_ts") else None
        age_s = f"{age}s" if age is not None and age < 600 else ("-" if age is None else f"{age // 60}m")
        up = st.get("up") or 0
        down = st.get("down") or 0
        print(f"{m['id']:<18}{state.get('rows') or 0:>7}{state.get('total') or 0:>7}"
              f"{st.get('avg_pct') or 0:>8.2f}{f'{up}/{down}':>12}{age_s:>7}  "
              f"{'ok' if state.get('last_ok') else (state.get('last_error') or 'pending')}")
    sig = db.signal_summary()
    window = sig.get("window") or {}
    print(f"signals  : {window.get('total') or 0} rows, {len(sig.get('sources') or {})} sources today, "
          f"{window.get('from')} -> {window.get('to')} (keep {SETTINGS.signal_retention_days}d)")
    runs = db.recent_runs(3)
    if runs:
        print("\nrecent runs:")
        for r in runs:
            print(f"  #{r['id']} {r['markets']} markets, {r['rows']} rows, "
                  f"{r['errors']} errors, {r['duration']}s")
    return 0


def cmd_proxy(args) -> int:
    from proxy_pool import ProxyPool

    db = Database(SETTINGS.db_path)
    pool = ProxyPool(SETTINGS, db=db, logger=logging.getLogger("em.proxy"))
    if not pool.enabled:
        print("proxy disabled or credentials missing (.env: dmdaili_apikey / dmdaili_pwd)")
        return 1
    batch = args.batch or SETTINGS.proxy_batch
    SETTINGS.proxy_batch = batch
    n = pool.refresh(force=True)
    print(json.dumps({"extracted": n, **pool.status()}, ensure_ascii=False, indent=2))
    return 0 if n else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="EastMoney global market monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create schema and seed markets")
    p_init.add_argument("--proxy-test", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_collect = sub.add_parser("collect", help="run one collection pass")
    p_collect.add_argument("--only", help="comma separated market ids")
    p_collect.add_argument("--klines", action="store_true")
    p_collect.add_argument("--proxy-test", action="store_true")
    p_collect.set_defaults(func=cmd_collect)

    p_serve = sub.add_parser("serve", help="serve API + dashboard")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="collector + dashboard")
    p_run.add_argument("--host")
    p_run.add_argument("--port", type=int)
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="print collection status")
    p_status.set_defaults(func=cmd_status)

    p_proxy = sub.add_parser("proxy", help="proxy pool utilities")
    p_proxy.add_argument("--test", action="store_true")
    p_proxy.add_argument("--batch", type=int)
    p_proxy.set_defaults(func=cmd_proxy)

    args = parser.parse_args(argv)
    setup_logging(SETTINGS.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
