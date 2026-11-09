"""Authoritative UTC clock.

The host clock is never trusted for market-session decisions: on every sync we
ask an internet time source for UTC, store the delta against the local clock,
and afterwards advance that anchor with a monotonic timer so a drifting or
misconfigured PC clock cannot move a market's open/close.
"""

from __future__ import annotations

import email.utils
import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone, tzinfo

log = logging.getLogger("em.clock")

SOURCES = (
    ("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
    ("timeapi", "https://timeapi.io/api/Time/current/zone?timeZone=UTC"),
    ("eastmoney", "https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f1&secids=1.000001"),
)


class Clock:
    """UTC now = anchor + monotonic elapsed, refreshed from internet sources."""

    def __init__(self, max_age: float = 600.0):
        self.max_age = max_age
        self._lock = threading.RLock()
        self._offset = 0.0            # server_epoch - local_epoch
        self._synced_at = 0.0         # monotonic anchor
        self._server_epoch = 0.0
        self._source = "local"
        self._samples = 0
        self._error: str | None = None
        self._stop = threading.Event()

    # -- probing ---------------------------------------------------------
    def _fetch(self, name: str, url: str) -> float | None:
        req = urllib.request.Request(url, headers={
            "User-Agent": "eastmoney-monitor/1.0",
            "Cache-Control": "no-cache",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            date_header = resp.headers.get("Date")
        if name == "cloudflare":
            for line in body.splitlines():
                if line.startswith("ts="):
                    return float(line[3:])
        elif name == "timeapi":
            payload = json.loads(body[:2000])
            dt = datetime(payload["year"], payload["month"], payload["day"],
                          payload["hour"], payload["minute"], payload["seconds"],
                          tzinfo=timezone.utc)
            return dt.timestamp() + payload.get("milliSeconds", 0) / 1000.0
        elif date_header:
            parsed = email.utils.parsedate_to_datetime(date_header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    def sync(self) -> bool:
        for name, url in SOURCES:
            before = time.monotonic()
            try:
                server_epoch = self._fetch(name, url)
            except Exception as exc:  # network or parse failure, try the next source
                log.debug("clock source %s failed: %s", name, exc)
                continue
            after = time.monotonic()
            if not server_epoch:
                continue
            # compensate the round trip by anchoring at the midpoint
            local_epoch = time.time()
            rtt = after - before
            with self._lock:
                self._server_epoch = server_epoch
                self._offset = server_epoch - (local_epoch - rtt / 2)
                self._synced_at = after
                self._source = name
                self._samples += 1
                self._error = None
            log.info("clock synced via %s (offset %+.2fs, rtt %.0fms)",
                     name, self._offset, rtt * 1000)
            return True
        with self._lock:
            self._error = "all time sources unreachable"
        log.warning("clock sync failed: all sources unreachable")
        return False

    # -- reading ---------------------------------------------------------
    def now(self) -> float:
        with self._lock:
            offset = self._offset
            fresh = time.monotonic() - self._synced_at < self.max_age
        if not fresh:
            return time.time() + offset
        return time.time() + offset

    def now_dt(self, tz: tzinfo = timezone.utc) -> datetime:
        return datetime.fromtimestamp(self.now(), tz)

    def stale(self) -> bool:
        with self._lock:
            return time.monotonic() - self._synced_at >= self.max_age

    def status(self) -> dict:
        with self._lock:
            local = time.time()
            age = time.monotonic() - self._synced_at if self._synced_at else None
            return {
                "source": self._source,
                "server_utc": datetime.fromtimestamp(self._server_epoch or local, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local_utc": datetime.fromtimestamp(local, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "offset_seconds": round(self._offset, 3),
                "age_seconds": int(age) if age is not None else None,
                "samples": self._samples,
                "error": self._error,
                "trusted": age is not None and age < self.max_age,
            }

    # -- background refresh ----------------------------------------------
    def start(self, interval: float = 300.0) -> None:
        def _loop():
            while not self._stop.is_set():
                if self.stale() or self._samples == 0:
                    self.sync()
                self._stop.wait(interval)

        threading.Thread(target=_loop, name="clock", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
