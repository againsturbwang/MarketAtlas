"""Collection engine: pull every market, normalise, persist, schedule."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from em_client import EastMoneyClient, EastMoneyError, num
from markets import KLINE_WATCH, MARKETS, Market
import sessions

log = logging.getLogger("em.collector")

KIND_LIMIT = {"stock": 9.8, "fund": 9.8, "bond": 19.5, "sector": 9.8}


def normalise(diff: list[dict], market: Market) -> list[dict]:
    rows: list[dict] = []
    for d in diff:
        code = d.get("f12")
        mkt = d.get("f13")
        if not code or mkt is None:
            continue
        price = num(d.get("f2"))
        if price is None and num(d.get("f18")) is None:
            continue
        secid = f"{mkt}.{code}"
        extra = {
            "vr": num(d.get("f10")),
            "fcap": num(d.get("f21")),
            "inflow": num(d.get("f62")),
            "pe_ttm": num(d.get("f115")),
            "dec": d.get("f1"),
            "ts": d.get("f124"),
        }
        rows.append({
            "secid": secid,
            "mkt": int(mkt),
            "code": str(code),
            "name": d.get("f14") or str(code),
            "price": price,
            "pct": num(d.get("f3")),
            "chg": num(d.get("f4")),
            "volume": num(d.get("f5")),
            "amount": num(d.get("f6")),
            "amplitude": num(d.get("f7")),
            "turnover": num(d.get("f8")),
            "pe": num(d.get("f9")),
            "high": num(d.get("f15")),
            "low": num(d.get("f16")),
            "open": num(d.get("f17")),
            "prev_close": num(d.get("f18")),
            "mcap": num(d.get("f20")),
            "pb": num(d.get("f23")),
            "extra": {k: v for k, v in extra.items() if v is not None},
        })
    return rows


def summarise(rows: list[dict], market: Market) -> dict:
    priced = [r for r in rows if r["pct"] is not None]
    up = sum(1 for r in priced if r["pct"] > 0)
    down = sum(1 for r in priced if r["pct"] < 0)
    flat = len(priced) - up - down
    limit = KIND_LIMIT.get(market.kind, 10.0)
    avg = round(sum(r["pct"] for r in priced) / len(priced), 3) if priced else 0.0
    amount = sum(r["amount"] or 0 for r in rows)
    ranked = sorted(priced, key=lambda r: r["pct"], reverse=True)
    top = ranked[:1]
    bottom = ranked[-1:]
    pick = lambda r: {"secid": r["secid"], "code": r["code"], "name": r["name"],
                      "pct": r["pct"], "price": r["price"]}
    return {
        "total": len(rows),
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": sum(1 for r in priced if r["pct"] >= limit),
        "limit_down": sum(1 for r in priced if r["pct"] <= -limit),
        "avg_pct": avg,
        "amount": amount,
        "top_gain": pick(top[0]) if top else None,
        "top_loss": pick(bottom[0]) if bottom else None,
    }


class Collector:
    def __init__(self, settings, db, client: EastMoneyClient, pool=None, clock=None):
        self.cfg = settings
        self.db = db
        self.client = client
        self.pool = pool
        self.clock = clock
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.states: dict[str, dict] = {}

    def now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    # -- one market ------------------------------------------------------
    def collect_market(self, market: Market, ts: int | None = None) -> dict:
        ts = ts or int(self.now())
        started = time.time()
        rows: list[dict] = []
        total = 0
        pages = 1
        error: str | None = None
        size = self.cfg.page_size

        def fetch(page: int):
            return self.client.clist(market.fs, page=page, size=size, fid="f12", po=0)

        try:
            first = fetch(1)
        except EastMoneyError as exc:
            error = str(exc)
            log.warning("market %s page 1 failed: %s", market.id, exc)
            first = {"total": 0, "diff": []}
        total = first["total"] or len(first["diff"])
        rows.extend(normalise(first["diff"], market))
        pages = max(1, math.ceil(total / size)) if total else 1
        if len(first["diff"]) < size:
            pages = 1
        remaining = list(range(2, min(pages, self.cfg.max_pages) + 1))
        if remaining:
            failed: list[int] = []
            workers = min(6, len(remaining))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(fetch, p): p for p in remaining}
                for fut in as_completed(futures):
                    page_no = futures[fut]
                    try:
                        payload = fut.result()
                    except EastMoneyError as exc:
                        error = error or str(exc)
                        failed.append(page_no)
                        log.warning("market %s page %d failed: %s", market.id, page_no, exc)
                        continue
                    rows.extend(normalise(payload["diff"], market))
            for page_no in failed:
                try:
                    payload = fetch(page_no)
                    rows.extend(normalise(payload["diff"], market))
                except EastMoneyError:
                    log.warning("market %s page %d lost", market.id, page_no)
        rows = list({r["secid"]: r for r in rows}.values())
        stored = 0
        if rows:
            stored = self.db.upsert_market_rows(market.id, market.kind, rows, ts, market.history)
            stats = summarise(rows, market)
            self.db.record_stats(market.id, ts, stats)
        duration = time.time() - started
        ok = bool(rows) and error is None
        self.db.record_state(market.id, ts, ok, error, stored, duration, total or len(rows))
        if not ok:
            self.db.log_event("warn", market.id, error or "no rows returned")
        log.info("market %-18s rows=%-6d total=%-6d pages=%-3d %.1fs%s",
                 market.id, stored, total, pages, duration, f" ERR {error}" if error else "")
        return {"market": market.id, "rows": stored, "total": total or len(rows),
                "pages": pages, "duration": round(duration, 2), "ok": ok, "error": error}

    # -- a full pass -----------------------------------------------------
    def run_once(self, only: list[str] | None = None, force: bool = False) -> dict:
        started = int(self.now())
        markets = [m for m in MARKETS if not only or m.id in only]
        now = self.now()
        due: list[Market] = []
        states: dict[str, dict] = {}
        db_states = self.db.collect_states()
        for m in markets:
            last = (db_states.get(m.id, {}) or {}).get("last_ts") or 0
            hit, st = sessions.should_collect(m.session, now, last, m.interval)
            states[m.id] = st
            if not hit:
                if st["open"]:
                    # a forced pass refreshes whatever is trading right now
                    hit = force or (now - last >= m.interval)
                else:
                    # never collected, or a day stale: worth one snapshot
                    hit = last == 0 or (now - last) > 86400
            if hit:
                due.append(m)
        self.states = states
        if not due:
            return {"run_id": None, "markets": 0, "ok": 0, "failed": 0, "rows": 0,
                    "duration": 0.0, "requests": self.client.stats["requests"],
                    "errors": self.client.stats["errors"], "skipped": True,
                    "open_markets": sum(1 for s in states.values() if s.get("open"))}
        run_id = self.db.start_run(started)
        results: list[dict] = []
        t0 = time.time()
        if due:
            with ThreadPoolExecutor(max_workers=max(1, min(self.cfg.workers, len(due)))) as pool:
                futures = {pool.submit(self.collect_market, m): m for m in due}
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # never let one market kill the run
                        market = futures[fut]
                        log.exception("market %s crashed", market.id)
                        results.append({"market": market.id, "rows": 0, "ok": False,
                                        "error": f"{type(exc).__name__}: {exc}"})
        duration = time.time() - t0
        ok_count = sum(1 for r in results if r["ok"])
        failed = [r for r in results if not r["ok"]]
        summary = {
            "run_id": run_id,
            "markets": len(results),
            "ok": ok_count,
            "failed": len(failed),
            "rows": sum(r["rows"] for r in results),
            "duration": round(duration, 2),
            "requests": self.client.stats["requests"],
            "errors": self.client.stats["errors"],
        }
        detail = json.dumps([{"m": r["market"], "e": r["error"]} for r in failed][:20],
                            ensure_ascii=False)
        self.db.finish_run(run_id, ok=not failed, markets=len(results),
                           requests=self.client.stats["requests"], rows=summary["rows"],
                           errors=len(failed), duration=duration, detail=detail)
        log.info("run #%d: %d markets, %d ok, %d failed, %d rows, %.1fs",
                 run_id, len(results), ok_count, len(failed), summary["rows"], duration)
        if results and (len(results) >= 3 or failed):
            self.db.log_event("info", "collector",
                              f"run #{run_id}: {ok_count}/{len(results)} markets, "
                              f"{summary['rows']} rows, {duration:.1f}s")
        if failed:
            self.db.log_event("warn", "collector",
                              f"{len(failed)} market(s) failed: "
                              + ", ".join(r["market"] for r in failed[:6]))
        return summary

    # -- watchlist -------------------------------------------------------
    def refresh_watchlist(self, force: bool = False) -> int:
        """Keep watched instruments fresh and store their intraday series."""
        last = float(self._meta_get("watch_ts") or 0)
        if not force and self.now() - last < 30:
            return 0
        items = self.db.watchlist()
        if not items:
            return 0
        secids = [w["secid"] for w in items][:60]
        self._meta_set("watch_ts", str(int(self.now())))
        try:
            quotes = self.client.watch_quotes(secids)
        except EastMoneyError as exc:
            log.warning("watchlist refresh failed: %s", exc)
            return 0
        ts = int(self.now())
        rows = []
        for secid, d in quotes.items():
            price = num(d.get("f2"))
            if price is None and num(d.get("f18")) is None:
                continue
            rows.append({
                "secid": secid,
                "mkt": d.get("f13"),
                "code": str(d.get("f12") or secid.split(".")[-1]),
                "name": d.get("f14"),
                "price": price,
                "pct": num(d.get("f3")),
                "chg": num(d.get("f4")),
                "volume": num(d.get("f5")),
                "amount": num(d.get("f6")),
                "high": num(d.get("f15")),
                "low": num(d.get("f16")),
                "open": num(d.get("f17")),
                "prev_close": num(d.get("f18")),
                "turnover": num(d.get("f8")),
                "extra": {"vr": num(d.get("f10"))},
            })
        if not rows:
            return 0
        stored = self.db.upsert_market_rows("watchlist", "stock", rows, ts, keep_history=True)
        log.info("watchlist: refreshed %d instruments", stored)
        return stored

    # -- daily bars ------------------------------------------------------
    def refresh_klines(self, force: bool = False) -> int:
        last = float(self._meta_get("kline_ts") or 0)
        if not force and self.now() - last < self.cfg.kline_refresh_hours * 3600:
            return 0
        written = 0
        for secid, name in KLINE_WATCH:
            try:
                bars = self.client.kline(secid, klt=101, limit=250)
                written += self.db.save_kline(secid, 101, bars)
                log.info("kline %s (%s): %d bars", secid, name, len(bars))
            except EastMoneyError as exc:
                log.warning("kline %s failed: %s", secid, exc)
            time.sleep(0.15)
        self._meta_set("kline_ts", str(int(self.now())))
        if written:
            self.db.log_event("info", "kline", f"refreshed {written} daily bars")
        return written

    # -- scheduler -------------------------------------------------------
    def loop(self) -> None:
        log.info("collector loop started (%d markets)", len(MARKETS))
        first = True
        while not self.stop_event.is_set():
            try:
                self.run_once(force=first)
                first = False
                self.refresh_watchlist()
                self.refresh_klines()
                if self.now() - float(self._meta_get("prune_ts") or 0) > 900:
                    removed = self.db.prune(self.cfg.retention_hours)
                    self._meta_set("prune_ts", str(int(self.now())))
                    if any(removed.values()):
                        log.info("prune: %s", removed)
            except Exception:
                log.exception("collector loop error")
                self.db.log_event("error", "collector", "cycle crashed, see logs")
            # when every market is shut there is nothing to poll; idle cheaply
            quiet = not any(s.get("open") for s in self.states.values())
            self.stop_event.wait(self.cfg.idle_sleep if quiet else self.cfg.cycle_sleep)
        log.info("collector loop stopped")

    # -- meta helpers ----------------------------------------------------
    def _meta_get(self, key: str):
        row = self.db.reader().execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self.db.set_meta(key, value)
