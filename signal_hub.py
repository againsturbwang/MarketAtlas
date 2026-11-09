"""Scheduler for the T+1 signal feeds.

Intraday feeds (limit-up pools, capital flow, connect, popularity) refresh while
the A-share market is live; once-a-day feeds (dragon-tiger, margin, research)
run after the 17:30 publish window. Everything is stored by trading day and
pruned after seven days.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import feeds
import sessions

log = logging.getLogger("em.signals")

CN_TZ = timezone(timedelta(hours=8))
DAILY_PUBLISH_HOUR = 17      # dragon-tiger and margin land in the evening
INTRADAY_WINDOW = (8 * 60 + 30, 16 * 60)   # CN local minutes


class SignalHub:
    def __init__(self, settings, db, client, clock=None, pool=None):
        self.cfg = settings
        self.db = db
        self.client = client
        self.clock = clock
        self.pool = pool
        self.stop_event = threading.Event()
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.stats = {"runs": 0, "rows": 0, "errors": 0, "last_feed": None}

    # -- helpers ---------------------------------------------------------
    def now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def cn_now(self) -> datetime:
        return datetime.fromtimestamp(self.now(), CN_TZ)

    def trading_day(self) -> str:
        return self.cn_now().strftime("%Y-%m-%d")

    def _due(self, feed: feeds.Feed, now: float, force: bool) -> bool:
        if force:
            return True
        last = self._last.get(feed.id, 0)
        if feed.kind == "daily":
            local = self.cn_now()
            published = local.hour * 60 + local.minute >= DAILY_PUBLISH_HOUR * 60
            # once a day, only after the data is published
            return published and (now - last > 6 * 3600)
        start, end = INTRADAY_WINDOW
        local = self.cn_now()
        minutes = local.hour * 60 + local.minute
        if local.weekday() >= 5:
            return False
        if not (start <= minutes <= end):
            # one refresh shortly after the close is handled by the daily window
            return False
        return now - last >= feed.interval

    # -- one feed --------------------------------------------------------
    def refresh(self, feed: feeds.Feed, force: bool = False) -> dict:
        day = self.trading_day()
        started = time.time()
        try:
            records = feeds.FETCHERS[feed.id](self.client, day)
        except Exception as exc:
            self.db.signal_state_put(feed.id, False, 0, f"{type(exc).__name__}: {exc}")
            log.warning("feed %s failed: %s", feed.id, exc)
            with self._lock:
                self.stats["errors"] += 1
            return {"feed": feed.id, "ok": False, "rows": 0, "error": str(exc)}
        if feed.id in ("limit_up", "limit_down"):
            records = [r for r in records if r["source"] == feed.id]
        stored = self.db.upsert_signals(feed.id, records)
        self.db.signal_state_put(feed.id, True, stored, None)
        self._last[feed.id] = self.now()
        with self._lock:
            self.stats["runs"] += 1
            self.stats["rows"] += stored
            self.stats["last_feed"] = feed.id
        log.info("signal %-14s %4d records in %.1fs", feed.id, stored, time.time() - started)
        return {"feed": feed.id, "ok": True, "rows": stored}

    # -- scheduler -------------------------------------------------------
    def run_due(self, force: bool = False) -> list[dict]:
        now = self.now()
        results = []
        for feed in feeds.FEEDS:
            if not self._due(feed, now, force):
                continue
            results.append(self.refresh(feed))
            time.sleep(0.2)
        return results

    def loop(self) -> None:
        log.info("signal hub started (%d feeds)", len(feeds.FEEDS))
        first = True
        while not self.stop_event.is_set():
            try:
                self.run_due(force=first)
                first = False
                if self.now() - getattr(self, "_pruned", 0) > 3600:
                    removed = self.db.prune_signals(self.cfg.signal_retention_days)
                    self._pruned = self.now()
                    if removed:
                        log.info("signal retention: dropped %d rows", removed)
            except Exception:
                log.exception("signal hub error")
            self.stop_event.wait(30)

    def start(self) -> None:
        threading.Thread(target=self.loop, name="signals", daemon=True).start()

    def status(self) -> dict:
        states = self.db.signal_states()
        return {
            "feeds": [{"id": f.id, "name": f.name, "kind": f.kind, "interval": f.interval,
                       "description": f.description,
                       "state": states.get(f.id)} for f in feeds.FEEDS],
            "stats": self.stats,
            "retention_days": self.cfg.signal_retention_days,
            "paused": self.stop_event.is_set(),
        }
