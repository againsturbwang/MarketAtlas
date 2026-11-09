"""dmdaili dynamic proxy pool.

Provider contract (https://www.dmdaili.com/develop):
    GET http://api.dmdaili.com/dmgetip.asp
        apikey, pwd, getnum<=200, httptype(1 http|2 https|3 socks5|4 auto),
        geshi(1 txt|2 json), operate=all|region, setcity, fenge
    -> {"code":0,"data":[{"ip":..,"port":..,"endtime":"YYYY/M/D HH:MM:SS"}],...}

The IPs this account hands out live about one minute, so the pool is a short
lived cache: fetch a small batch, validate lazily against real traffic, rotate
on failure, and never extract faster than the provider's comfort threshold.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

CODE_MESSAGES = {
    0: "ok",
    1: "rate limited by provider (slow down)",
    200: "ok",
    600: "extracting too fast",
    601: "plan expired",
    602: "pwd is invalid",
    603: "apikey is empty",
    604: "region unavailable",
    605: "whitelisting in progress (~1 min)",
    606: "traffic quota exhausted",
    399: "caller IP is not whitelisted",
    401: "caller IP is not whitelisted",
    402: "concurrent access rejected",
    411: "target host not found",
    412: "DNS resolution failed",
    413: "target not allowed",
    414: "target connect failed",
    417: "api traffic limit reached",
}


@dataclass(slots=True)
class Proxy:
    ip: str
    port: int
    expires_at: int
    scheme: str = "http"
    ok: int = 0
    fail: int = 0
    latency: float = 0.0
    uses: int = 0
    last_error: str | None = None
    last_used: float = 0.0

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.ip}:{self.port}"

    @property
    def key(self) -> str:
        return f"{self.ip}:{self.port}"

    def alive(self, margin: int = 5) -> bool:
        return self.expires_at - margin > time.time()

    def score(self) -> float:
        """Lower is better: fresh IPs with a good success rate and low latency."""
        return (self.fail + 1) / (self.ok + 1) + self.latency * 0.5


class ProxyPool:
    def __init__(self, settings, db=None, logger=None):
        self.cfg = settings
        self.db = db
        self.log = logger
        self._lock = threading.RLock()
        self._pool: list[Proxy] = []
        self._last_fetch = 0.0
        self._last_error: str | None = None
        self._disabled_until = 0.0
        self._cursor = 0
        self._stop = threading.Event()
        self.counters = {"fetched": 0, "extract_calls": 0, "failures": 0, "success": 0,
                         "fallback_direct": 0}

    # -- provider --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.cfg.proxy_enabled and self.cfg.proxy_configured

    def _fetch(self) -> list[Proxy]:
        want = max(self.cfg.proxy_batch, self.cfg.proxy_target - self.live_count())
        params = {
            "apikey": self.cfg.proxy_apikey,
            "pwd": self.cfg.proxy_pwd,
            "getnum": str(max(1, min(200, want))),
            "httptype": "4",
            "geshi": "2",
            "fenge": "1",
            "operate": self.cfg.proxy_country or "all",
        }
        url = f"{self.cfg.proxy_api}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "eastmoney-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8", "replace")
        self.counters["extract_calls"] += 1
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise RuntimeError(f"proxy API returned non-json: {payload[:120]}") from exc
        code = int(data.get("code") or 0)
        if code not in (0, 200) or not data.get("data"):
            msg = CODE_MESSAGES.get(code) or (data.get("msg") or "extraction failed")
            raise RuntimeError(f"proxy API code {code}: {msg}")
        out: list[Proxy] = []
        now = int(time.time())
        for item in data["data"]:
            try:
                exp = time.mktime(time.strptime(item["endtime"], "%Y/%m/%d %H:%M:%S"))
            except (KeyError, ValueError):
                exp = now + 60
            out.append(Proxy(ip=item["ip"], port=int(item["port"]), expires_at=int(exp),
                             scheme=self.cfg.proxy_scheme))
        return out

    def refresh(self, force: bool = False) -> int:
        with self._lock:
            now = time.time()
            if not force and now - self._last_fetch < self.cfg.proxy_min_gap:
                return 0
            if now < self._disabled_until:
                return 0
            self._last_fetch = now
            try:
                fresh = self._fetch()
            except Exception as exc:  # provider or network problem
                self._last_error = str(exc)
                self.counters["failures"] += 1
                self._disabled_until = now + 30
                if self.log:
                    self.log.warning("proxy extraction failed: %s", exc)
                if self.db:
                    self.db.log_event("warn", "proxy", f"extraction failed: {exc}")
                return 0
            self._last_error = None
            known = {p.key: p for p in self._pool}
            for p in fresh:
                prev = known.get(p.key)
                if prev:
                    prev.expires_at = max(prev.expires_at, p.expires_at)
                else:
                    self._pool.append(p)
            self._pool = [p for p in self._pool if p.alive(self.cfg.proxy_ttl_margin)]
            self.counters["fetched"] += len(fresh)
            if self.db:
                now_i = int(now)
                self.db.save_proxies([
                    {"ip": p.ip, "port": p.port, "scheme": p.scheme,
                     "fetched_at": now_i, "expires_at": p.expires_at} for p in fresh
                ])
            if self.log:
                self.log.info("proxy pool: +%d (live=%d)", len(fresh), len(self._pool))
            return len(fresh)

    # -- selection -------------------------------------------------------
    def acquire(self, exclude: set[str] | None = None) -> Proxy | None:
        if not self.enabled:
            return None
        exclude = exclude or set()
        with self._lock:
            candidates = [p for p in self._pool
                          if p.alive(self.cfg.proxy_ttl_margin) and p.key not in exclude]
            if not candidates:
                self.refresh(force=not self._pool)
                candidates = [p for p in self._pool
                              if p.alive(self.cfg.proxy_ttl_margin) and p.key not in exclude]
            if not candidates:
                return None
            candidates.sort(key=lambda p: p.score())
            # spread load over the best 60% instead of hammering a single IP
            pick_from = candidates[: max(1, int(len(candidates) * 0.6))]
            proxy = pick_from[self._cursor % len(pick_from)]
            self._cursor += 1
            proxy.last_used = time.time()
            return proxy

    def report(self, proxy: Proxy, ok: bool, error: str | None = None,
               latency: float | None = None) -> None:
        with self._lock:
            proxy.uses += 1
            if latency is not None:
                proxy.latency = latency if proxy.latency == 0 else proxy.latency * 0.7 + latency * 0.3
            if ok:
                proxy.ok += 1
                self.counters["success"] += 1
            else:
                proxy.fail += 1
                proxy.last_error = error
                self.counters["failures"] += 1
                if proxy.fail >= 3:
                    self._pool = [p for p in self._pool if p.key != proxy.key]
            if self.db:
                self.db.report_proxy(proxy.ip, proxy.port, ok, error)

    def maintain(self, stop_event: threading.Event | None = None,
                 interval: float = 1.0) -> None:
        """Keep the pool topped up: the provider hands out IPs that live 1-3 min."""
        stop_event = stop_event or self._stop
        while not stop_event.is_set():
            if self.enabled:
                live = self.live_count()
                if live < self.cfg.proxy_target:
                    self.refresh(force=True)
            stop_event.wait(interval)

    def live_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._pool if p.alive(self.cfg.proxy_ttl_margin))

    def start(self) -> threading.Event:
        self._stop = threading.Event()
        threading.Thread(target=self.maintain, args=(self._stop,), name="proxy-pool",
                         daemon=True).start()
        return self._stop

    def note_direct(self) -> None:
        self.counters["fallback_direct"] += 1

    # -- reporting -------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            live = [p for p in self._pool if p.alive(self.cfg.proxy_ttl_margin)]
            return {
                "enabled": self.enabled,
                "configured": self.cfg.proxy_configured,
                "live": len(live),
                "tracked": len(self._pool),
                "next_expiry": min((p.expires_at for p in live), default=None),
                "best": [{"proxy": p.key, "expires_in": int(p.expires_at - time.time()),
                          "ok": p.ok, "fail": p.fail} for p in
                         sorted(live, key=lambda p: p.score())[:5]],
                "last_error": self._last_error,
                "disabled_for": max(0, int(self._disabled_until - time.time())),
                "counters": dict(self.counters),
            }
