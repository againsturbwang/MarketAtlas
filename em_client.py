"""EastMoney push2 HTTP client: proxy-aware, rate limited, retrying."""

from __future__ import annotations

import gzip
import http.client
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from markets import QUOTE_FIELDS

log = logging.getLogger("em.client")

UT = "fa5fd1943c7b386f172d6893dbfba10b"
BASE_HTTP = "http://{host}"
BASE_HTTPS = "https://{host}"

# Field bundle used by the quote-centre stock page itself. Order book (f11-f40)
# is only returned when the full bundle is requested together with wbp2u.
STOCK_FIELDS = (
    "f58,f107,f57,f43,f59,f169,f301,f60,f170,f152,f177,f111,f46,f44,f45,f47,f48,"
    "f19,f17,f15,f13,f11,f20,f18,f16,f14,f12,f39,f37,f35,f33,f31,f40,f38,f36,f34,f32,"
    "f531,"  # asking for f531 is what makes push2 include the five-level book
    "f161,f49,f171,f50,f86,f84,f85,f168,f108,f116,f167,f164,f162,f163,f92,f71,f117,"
    "f292,f51,f52,f191,f192,f127,f260,f261,f279,f277,f278,f288,f211,f212,f213,f214,"
    "f215,f210,f209,f208,f207,f206,f262,f294,f181,f295,f269,f270,f256,f257,f285,f286,f734"
)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36",
)


class EastMoneyError(RuntimeError):
    """Any failure that prevented a usable payload from EastMoney."""


class RateLimiter:
    """Simple token bucket shared by all worker threads."""

    def __init__(self, rate: float):
        self.rate = max(0.5, rate)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                delay = self._next - now
                self._next += 1.0 / self.rate
            else:
                delay = 0.0
                self._next = now + 1.0 / self.rate
        if delay > 0:
            time.sleep(delay)


class EastMoneyClient:
    def __init__(self, settings, pool=None):
        self.cfg = settings
        self.pool = pool
        self.limiter = RateLimiter(settings.rate_limit)
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "ok": 0, "errors": 0, "bytes": 0,
                      "proxy": 0, "direct": 0, "retries": 0}

    # -- transport -------------------------------------------------------
    def _opener(self, proxy_url: str | None):
        handlers = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _decode(self, resp) -> str:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        with self._lock:
            self.stats["bytes"] += len(raw)
        charset = resp.headers.get_content_charset() or "utf-8"
        if charset.lower() in ("gb2312", "gbk", "gb18030", "gb-2312"):
            charset = "gb18030"
        return raw.decode(charset, "replace")

    def get(self, path: str, params: dict, *, host: str = "push2.eastmoney.com",
            allow_direct: bool = True, hedge_ms: int = 0, attempts: int | None = None,
            text: bool = False) -> str:
        """Fetch a payload.

        `hedge_ms` > 0 makes this an interactive request: if the first proxy has
        not answered within that budget, the same call is fired again through a
        different IP and whichever returns first wins. Cheap insurance against
        the occasional stalled proxy when a user is waiting on a click.
        """
        total = max(1, attempts or self.cfg.max_retries)
        if hedge_ms > 0 and total > 1:
            return self._hedged(path, params, host, allow_direct, hedge_ms, total)
        return self._attempts(path, params, host, allow_direct, total, text=text)

    def _hedged(self, path, params, host, allow_direct, hedge_ms, total) -> str:
        split = max(1, total // 2)
        first_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=2) as pool:
            primary = pool.submit(self._attempts, path, params, host, allow_direct, split)
            time.sleep(hedge_ms / 1000.0)
            backup = pool.submit(self._attempts, path, params, host, allow_direct,
                                 max(1, total - split))
            for fut in as_completed([primary, backup]):
                try:
                    return fut.result()
                except EastMoneyError as exc:
                    first_error = first_error or exc
        raise EastMoneyError(str(first_error or f"GET {host}{path} failed"))

    def _attempts(self, path: str, params: dict, host: str, allow_direct: bool,
                  attempts: int, *, data: bytes | None = None,
                  method: str = "GET", text: bool = False) -> str:
        query = urllib.parse.urlencode(params, safe=":,+")
        url_tail = f"{path}?{query}"
        last_error: Exception | None = None
        tried: set[str] = set()
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            proxy = self.pool.acquire(tried) if self.pool else None
            if proxy is not None:
                tried.add(proxy.key)
            # last resort: go direct rather than lose the market entirely
            if last_attempt and allow_direct and tried:
                proxy = None
            use_proxy = proxy is not None
            if not use_proxy and not allow_direct and tried:
                break
            scheme = "http" if use_proxy else "https"
            url = (BASE_HTTP if use_proxy else BASE_HTTPS).format(host=host) + "/" + url_tail.lstrip("/")
            self.limiter.wait()
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
                "Referer": "https://www.eastmoney.com/",
                "Connection": "close",
            }
            if data is not None:
                headers["Content-Type"] = "application/json"
                headers["Origin"] = "https://www.eastmoney.com"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            started = time.time()
            try:
                with self._opener(proxy.url if use_proxy else None).open(
                        req, timeout=self.cfg.request_timeout) as resp:
                    body = self._decode(resp)
                if text:
                    if not body or len(body) < 200:
                        raise EastMoneyError(f"short page: {body[:80]!r}")
                elif not body or body.lstrip()[:1] not in "{[":
                    raise EastMoneyError(f"unexpected payload: {body[:120]!r}")
                with self._lock:
                    self.stats["requests"] += 1
                    self.stats["ok"] += 1
                    self.stats["proxy" if use_proxy else "direct"] += 1
                if proxy is not None:
                    self.pool.report(proxy, True, latency=time.time() - started)
                return body
            except (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException,
                    TimeoutError, OSError, EastMoneyError) as exc:
                last_error = exc
                with self._lock:
                    self.stats["requests"] += 1
                    self.stats["errors"] += 1
                    if attempt:
                        self.stats["retries"] += 1
                if proxy is not None:
                    self.pool.report(proxy, False, str(exc))
                elif self.pool is not None:
                    self.pool.note_direct()
                log.debug("request failed (%s): %s", "proxy" if use_proxy else "direct", exc)
                time.sleep(min(1.5, 0.15 * (attempt + 1)))
            if not use_proxy and attempt + 1 < attempts:
                allow_direct = False
        raise EastMoneyError(f"GET {host}{path} failed: {last_error}")

    def get_text(self, path: str, params: dict, host: str) -> str:
        """Fetch an HTML page (news index, article) through the same pool."""
        return self.get(path, params, host=host, text=True)

    # -- generic helpers for the data-centre feeds -----------------------
    def get_url(self, path: str, params: dict, host: str, hedge_ms: int = 0) -> str:
        return self.get(path, params, host=host, hedge_ms=hedge_ms)

    def post_json(self, path: str, payload: dict, host: str, hedge_ms: int = 0) -> str:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if hedge_ms > 0:
            return self._hedged_post(path, data, host, hedge_ms)
        return self._attempts(path, {}, host, True, self.cfg.max_retries,
                              data=data, method="POST")

    def _hedged_post(self, path: str, data: bytes, host: str, hedge_ms: int) -> str:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self._attempts, path, {}, host, True, 2, data=data, method="POST")
            time.sleep(hedge_ms / 1000.0)
            second = pool.submit(self._attempts, path, {}, host, True, 1, data=data, method="POST")
            for fut in as_completed([first, second]):
                try:
                    return fut.result()
                except EastMoneyError:
                    continue
        raise EastMoneyError(f"POST {host}{path} failed")

    # -- API -------------------------------------------------------------
    def clist(self, fs: str, *, page: int = 1, size: int | None = None, fid: str = "f12",
              po: int = 0, fields: str = QUOTE_FIELDS, host: str = "push2.eastmoney.com") -> dict:
        params = {
            "pn": page,
            "pz": size or self.cfg.page_size,
            "po": po,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
            "ut": UT,
            "dect": 1,
            "wbp2u": "|0|0|0|web",
            "_": int(time.time() * 1000),
        }
        body = self.get("/api/qt/clist/get", params, host=host)
        return self._parse_clist(body)

    @staticmethod
    def _parse_clist(body: str) -> dict:
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise EastMoneyError(f"bad json: {body[:120]!r}") from exc
        data = payload.get("data")
        if not data:
            return {"total": 0, "diff": []}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        return {"total": int(data.get("total") or 0), "diff": diff}

    def kline(self, secid: str, *, klt: int = 101, limit: int = 240, hedge_ms: int = 0,
              fqt: int = 1) -> list[tuple]:
        # bound the window: with beg=0 EastMoney ignores lmt and returns the full listing
        beg = time.strftime("%Y%m%d", time.localtime(time.time() - 3 * 365 * 86400))
        params = {
            "secid": secid,
            "klt": klt,
            "fqt": fqt,
            "beg": beg,
            "end": 20500101,
            "lmt": limit,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "ut": UT,
        }
        body = self.get("/api/qt/stock/kline/get", params, host="push2his.eastmoney.com", hedge_ms=hedge_ms)
        payload = json.loads(body)
        data = payload.get("data") or {}
        out: list[tuple] = []
        for line in data.get("klines") or []:
            parts = line.split(",")
            if len(parts) < 8:
                continue
            try:
                out.append((
                    parts[0],
                    _num(parts[1]), _num(parts[2]), _num(parts[3]), _num(parts[4]),
                    _num(parts[5]), _num(parts[6]), _num(parts[8]),
                ))
            except (ValueError, IndexError):
                continue
        return out[-limit:] if limit else out

    def trends(self, secid: str, ndays: int = 1, hedge_ms: int = 0) -> dict:
        params = {
            "secid": secid,
            "ndays": ndays,
            "iscr": 0,
            "iscca": 0,
            "ut": UT,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        }
        body = self.get("/api/qt/stock/trends2/get", params, host="push2his.eastmoney.com", hedge_ms=hedge_ms)
        payload = json.loads(body)
        data = payload.get("data") or {}
        points = []
        for line in data.get("trends") or []:
            parts = line.split(",")
            if len(parts) >= 7:
                points.append({"t": parts[0], "price": _num(parts[2]), "pct": _num(parts[8])
                               if len(parts) > 8 else None})
        return {"name": data.get("name"), "preClose": data.get("preClose"), "points": points}

    # -- single stock terminal -------------------------------------------
    def stock(self, secid: str, hedge_ms: int = 0) -> dict:
        """Full single-instrument snapshot including the five-level book."""
        params = {
            "invt": 2,
            "fltt": 2,
            "dect": 1,
            "secid": secid,
            "ut": UT,
            "wbp2u": "|0|0|0|web",
            "fields": STOCK_FIELDS,
            "_": int(time.time() * 1000),
        }
        body = self.get("/api/qt/stock/get", params, hedge_ms=hedge_ms)
        payload = json.loads(body)
        data = payload.get("data")
        if not data:
            raise EastMoneyError(f"no snapshot for {secid}")
        return data

    def flow(self, secid: str, klt: int = 1, hedge_ms: int = 0) -> list[dict]:
        """Capital flow series: klt=1 intraday minutes, klt=101 daily.

        Daily history only exists on push2his; push2 returns today's single row.
        """
        params = {
            "lmt": 0,
            "klt": klt,
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56",
            "ut": UT,
        }
        host = "push2his.eastmoney.com" if klt >= 101 else "push2.eastmoney.com"
        body = self.get("/api/qt/stock/fflow/kline/get", params, host=host, hedge_ms=hedge_ms)
        payload = json.loads(body)
        out = []
        for line in (payload.get("data") or {}).get("klines") or []:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            out.append({
                "t": parts[0],
                "main": _num(parts[1]),
                "small": _num(parts[2]),
                "mid": _num(parts[3]),
                "big": _num(parts[4]),
                "xlarge": _num(parts[5]),
            })
        return out

    def trades(self, secid: str, pos: int = -40, hedge_ms: int = 0) -> list[dict]:
        """Tick prints (逐笔成交), newest last."""
        params = {
            "fields1": "f1,f2,f3,f4",
            "fields2": "f51,f52,f53,f54,f55",
            "fltt": 2,
            "pos": pos,
            "secid": secid,
            "ut": UT,
        }
        body = self.get("/api/qt/stock/details/get", params, hedge_ms=hedge_ms)
        payload = json.loads(body)
        data = payload.get("data") or {}
        out = []
        for line in data.get("details") or []:
            parts = line.split(",")
            if len(parts) < 4:
                continue
            out.append({"time": parts[0], "price": _num(parts[1]), "volume": _num(parts[2]),
                        "side": int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None})
        return out

    def watch_quotes(self, secids: list[str]) -> dict[str, dict]:
        """Batch quotes for the watchlist in one round trip."""
        if not secids:
            return {}
        body = self.get("/api/qt/ulist.np/get", {
            "fltt": 2, "invt": 2, "secids": ",".join(secids), "ut": UT,
            "fields": "f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f10,f8",
        })
        payload = json.loads(body)
        diff = ((payload.get("data") or {}).get("diff")) or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        return {f"{d.get('f13')}.{d.get('f12')}": d for d in diff}


def _num(value):
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def num(value):
    return _num(value)
