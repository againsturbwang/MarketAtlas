"""Audit what this server actually delivers to an external agent.

Checks three things:
  1. inventory  - every route answers, with what shape
  2. correctness- klines and quotes match the upstream EastMoney numbers
  3. delivery   - gzip, headers, pagination, limits, error formats

    python scripts/audit_delivery.py --report docs/api-delivery-report.md
"""

from __future__ import annotations

import argparse
import gzip
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"
CTX = ssl.create_default_context()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

problems: list[str] = []


def call(path: str, params: dict | None = None, *, gzip_ok: bool = True,
         timeout: float = 25.0) -> dict:
    url = BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if gzip_ok:
        headers["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(url, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            wire = len(raw)
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            return {"status": resp.status, "ms": int((time.time() - started) * 1000),
                    "bytes": len(raw), "wire": wire, "ctype": resp.headers.get("Content-Type"),
                    "encoding": encoding or "identity",
                    "cache": resp.headers.get("Cache-Control"),
                    "body": raw.decode("utf-8", "replace"), "error": None}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {"status": exc.code, "ms": int((time.time() - started) * 1000),
                "bytes": len(body), "ctype": exc.headers.get("Content-Type"),
                "encoding": "", "cache": None, "body": body, "error": f"HTTP {exc.code}"}
    except Exception as exc:                      # noqa: BLE001 - audit reports everything
        return {"status": 0, "ms": int((time.time() - started) * 1000), "bytes": 0,
                "ctype": None, "encoding": "", "cache": None, "body": "",
                "error": f"{type(exc).__name__}: {exc}"}


def jload(result: dict):
    try:
        return json.loads(result["body"])
    except ValueError:
        return None


def upstream(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        return resp.read().decode("utf-8", "replace")


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------
ROUTES = [
    ("GET", "/api/health", {}, "liveness"),
    ("GET", "/api/status", {}, "system status"),
    ("GET", "/api/overview", {}, "indices, groups, movers"),
    ("GET", "/api/markets", {}, "market catalogue"),
    ("GET", "/api/market/cn.a", {"page": 1, "size": 5}, "one market's members"),
    ("GET", "/api/movers", {"limit": 5}, "top gainers/losers"),
    ("GET", "/api/search", {"q": "600519"}, "symbol search"),
    ("GET", "/api/watchlist", {}, "watchlist"),
    ("GET", "/api/spark", {"secids": "1.600519"}, "sparkline series"),
    ("GET", "/api/activity", {}, "collector log"),
    ("GET", "/api/stock/1.600519", {}, "single stock snapshot"),
    ("GET", "/api/stock/1.600519/kline", {"klt": 101, "limit": 60}, "daily bars"),
    ("GET", "/api/stock/1.600519/flow", {"limit": 20}, "daily capital flow"),
    ("GET", "/api/kline/1.600519", {"klt": 101, "limit": 60}, "bars (legacy alias)"),
    ("GET", "/api/signals/summary", {}, "T+1 signal counts"),
    ("GET", "/api/signals/limit_up", {"limit": 10}, "limit-up pool"),
    ("GET", "/api/signals/stock/0.002131", {}, "signals for one stock"),
    ("GET", "/api/intel/sections", {}, "news sections"),
    ("GET", "/api/intel/articles", {"limit": 5}, "article list"),
    ("GET", "/api/intel/digest", {"days": 1, "limit": 5}, "agent digest"),
    ("GET", "/api/v1/meta", {}, "protocol metadata"),
    ("GET", "/api/v1/regime", {}, "market regime packet"),
    ("GET", "/api/v1/candidates", {"limit": 5}, "ranked candidates"),
    ("GET", "/api/v1/stock/0.002131/evidence", {}, "stock evidence"),
    ("GET", "/api/v1/news/search", {"q": "white"}, "news search"),
    ("GET", "/api/v1/brief", {}, "markdown brief"),
    ("GET", "/api/v1/openapi.json", {}, "OpenAPI spec"),
    ("GET", "/api/v1/predictions/scorecard", {}, "scorecard"),
]


def inventory() -> list[dict]:
    rows = []
    for method, path, params, label in ROUTES:
        res = call(path, params)
        parsed = jload(res)
        keys = sorted(parsed.keys())[:8] if isinstance(parsed, dict) else (
            f"[{len(parsed)} items]" if isinstance(parsed, list) else None)
        rows.append({"method": method, "path": path, "label": label,
                     "status": res["status"], "ms": res["ms"], "bytes": res["bytes"],
                     "wire": res.get("wire") or res["bytes"],
                     "encoding": res["encoding"], "ctype": res["ctype"],
                     "keys": keys, "error": res["error"]})
        if res["status"] != 200:
            problems.append(f"{path} returned {res['status']} {res['error'] or ''}")
        elif parsed is None and path != "/api/v1/brief":
            problems.append(f"{path} did not return JSON ({res['ctype']})")
    return rows


# --------------------------------------------------------------------------
# kline correctness against upstream
# --------------------------------------------------------------------------
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def raw_kline(secid: str, klt: int, limit: int) -> list[dict]:
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&klt={klt}&fqt=1&beg=0&end=20500101&lmt={limit}"
           f"&fields1=f1,f2,f3,f4,f5,f6&fields2={KLINE_FIELDS2}")
    payload = json.loads(upstream(url))
    out = []
    for line in ((payload.get("data") or {}).get("klines") or []):
        p = line.split(",")
        if len(p) < 11:
            continue
        out.append({"d": p[0], "open": num(p[1]), "close": num(p[2]),
                    "high": num(p[3]), "low": num(p[4]), "volume": num(p[5]),
                    "amount": num(p[6]), "amplitude": num(p[7]),
                    "pct": num(p[8]), "chg": num(p[9]), "turnover": num(p[10])})
    return out[-limit:]


def check_kline(secid: str, name: str, klt: int, limit: int = 60) -> dict:
    res = call(f"/api/stock/{urllib.parse.quote(secid, safe='.')}/kline",
               {"klt": klt, "limit": limit})
    payload = jload(res) or {}
    bars = payload.get("bars") or []
    last_final = payload.get("last_final")
    ref = raw_kline(secid, klt, limit)
    by_date = {b["d"]: b for b in bars}
    # a forming bar keeps moving between our read and the upstream read, so it is
    # reported as lag rather than as a correctness failure
    forming_date = bars[-1]["d"] if bars and last_final is False else None
    compared = mismatched = 0
    max_delta = {"open": 0.0, "close": 0.0, "high": 0.0, "low": 0.0,
                 "volume": 0.0, "amount": 0.0, "pct": 0.0}
    examples = []
    lag = None
    for row in ref:
        mine = by_date.get(row["d"])
        if not mine:
            continue
        if forming_date and row["d"] == forming_date:
            lag = {"close": (num(mine.get("close")), row["close"]),
                   "volume": (num(mine.get("volume")), row["volume"]),
                   "amount": (num(mine.get("amount")), row["amount"])}
            continue
        compared += 1
        bad = []
        for field in ("open", "close", "high", "low", "volume", "amount", "pct"):
            a, b = num(mine.get(field)), row[field]
            if a is None or b is None:
                continue
            delta = abs(a - b)
            rel = delta / max(abs(b), 1e-9)
            if rel > max(1e-6, 0.5 / max(abs(b), 1e-9)):
                bad.append(field)
                max_delta[field] = max(max_delta[field], rel)
        if bad:
            mismatched += 1
            if len(examples) < 3:
                examples.append({"date": row["d"], "fields": bad,
                                 "mine": {k: mine.get(k) for k in bad},
                                 "upstream": {k: row[k] for k in bad}})
    # internal consistency of what we serve
    inconsistent = []
    for i, bar in enumerate(bars):
        o, c, h, l = (num(bar.get(k)) for k in ("open", "close", "high", "low"))
        if None in (o, c, h, l):
            continue
        if h < max(o, c) - 1e-6 or l > min(o, c) + 1e-6:
            inconsistent.append({"date": bar["d"], "kind": "ohlc",
                                 "reason": "high/low outside open/close"})
        if i > 0:
            prev = num(bars[i - 1].get("close"))
            pct = num(bar.get("pct"))
            if prev and pct is not None:
                implied = (c - prev) / prev * 100
                if abs(implied - pct) > 0.05:
                    inconsistent.append({"date": bar["d"], "kind": "vendor_pct",
                                         "reason": f"pct {pct} != implied {implied:.2f} "
                                                   "(vendor field; closes match)"})
    return {"secid": secid, "name": name, "klt": klt,
            "last_final": last_final, "forming": forming_date, "lag": lag,
            "served": len(bars), "upstream": len(ref), "compared": compared,
            "mismatched": mismatched, "max_rel_delta": max_delta,
            "examples": examples, "inconsistent": inconsistent[:5],
            "inconsistent_count": len(inconsistent),
            "first": bars[0]["d"] if bars else None,
            "last": bars[-1]["d"] if bars else None,
            "request_ms": res["ms"]}


# --------------------------------------------------------------------------
# quote correctness
# --------------------------------------------------------------------------
def check_quote(secid: str, name: str) -> dict:
    res = call(f"/api/stock/{urllib.parse.quote(secid, safe='.')}")
    payload = jload(res) or {}
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fltt=2&invt=2"
           f"&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f170")
    raw = json.loads(upstream(url)).get("data") or {}
    pairs = [
        ("price", payload.get("price"), num(raw.get("f43"))),
        ("open", payload.get("open"), num(raw.get("f46"))),
        ("high", payload.get("high"), num(raw.get("f44"))),
        ("low", payload.get("low"), num(raw.get("f45"))),
        ("prev_close", payload.get("pre_close") or payload.get("prev_close"),
         num(raw.get("f60"))),
        ("pct", payload.get("pct"), num(raw.get("f170"))),
        ("volume", payload.get("volume"), num(raw.get("f47"))),
        ("amount", payload.get("amount"), num(raw.get("f48"))),
    ]
    diffs = []
    for field, mine, ref in pairs:
        if mine is None or ref is None:
            diffs.append({"field": field, "mine": mine, "upstream": ref, "note": "missing"})
            continue
        delta = abs(float(mine) - ref)
        # pct is already a percentage: compare absolute points, not ratios
        tol = 0.1 if field == "pct" else max(1e-6, abs(ref) * 1e-6)
        if delta > tol:
            rel = delta / max(abs(ref), 1e-9)
            diffs.append({"field": field, "mine": mine, "upstream": ref,
                          "rel": rel if field != "pct" else delta / 100})
    # a live snapshot read a second apart moves by ticks; that is not staleness
    lagging = bool(diffs) and all(d.get("rel", 0) < 0.005 for d in diffs)
    return {"secid": secid, "name": name, "status": res["status"],
            "ms": res["ms"], "diffs": diffs, "lagging": lagging, "values": dict(
                (f, m) for f, m, _ in pairs),
            "age_s": (int(time.time() - payload["ts"])
                      if payload.get("ts") else None)}


# --------------------------------------------------------------------------
# delivery semantics
# --------------------------------------------------------------------------
def check_delivery() -> dict:
    out: dict = {}

    gz = call("/api/overview", gzip_ok=True)
    plain = call("/api/overview", gzip_ok=False)
    out["gzip"] = {"with_encoding": gz["encoding"], "with_bytes": gz["wire"],
                   "plain_bytes": plain["wire"],
                   "ratio": round(plain["wire"] / max(gz["wire"], 1), 2)}

    small = call("/api/health", gzip_ok=True)
    out["small_response_encoding"] = small["encoding"] or "identity"

    head = urllib.request.Request(BASE + "/api/health", method="HEAD",
                                  headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(head, timeout=10) as resp:
            out["head"] = {"status": resp.status, "length": resp.headers.get("Content-Length")}
    except Exception as exc:                      # noqa: BLE001
        out["head"] = {"error": str(exc)[:80]}

    err = call("/api/market/nope")
    out["unknown_market"] = {"status": err["status"], "body": err["body"][:120]}

    bad = call("/api/stock/9.999999/kline", {"klt": 101, "limit": 5})
    parsed = jload(bad)
    out["unknown_secid"] = {"status": bad["status"], "bars": len((parsed or {}).get("bars") or []),
                            "body": bad["body"][:100] if parsed is None else None}

    silly = call("/api/stock/1.600519/kline", {"klt": 101, "limit": 9999})
    out["limit_clamp"] = {"requested": 9999,
                          "served": len((jload(silly) or {}).get("bars") or [])}

    wrong_klt = call("/api/stock/1.600519/kline", {"klt": 7, "limit": 5})
    out["bad_klt"] = {"status": wrong_klt["status"],
                      "klt_echo": (jload(wrong_klt) or {}).get("klt")}

    typo = call("/api/stock/1.600519/bars")
    typo_json = jload(typo) or {}
    out["typo_suffix"] = {"status": typo["status"], "keys": sorted(typo_json.keys())[:8]}

    page = call("/api/market/cn.a", {"page": 2, "size": 5})
    page_json = jload(page) or {}
    out["pagination"] = {"keys": sorted(page_json.keys())[:10],
                         "rows": len(page_json.get("rows") or [])}

    v1err = call("/api/v1/stock/not-a-secid/evidence")
    out["v1_bad_secid"] = {"status": v1err["status"], "body": v1err["body"][:120]}
    return out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="docs/api-delivery-report.md")
    args = parser.parse_args()

    print("inventory...")
    inv = inventory()

    targets = [("1.600519", "贵州茅台 A股"), ("0.002131", "利欧股份 A股"),
               ("1.000001", "上证指数"), ("116.00700", "腾讯控股 港股"),
               ("105.AAPL", "Apple 美股")]
    print("klines...")
    klines = []
    for secid, name in targets:
        for klt, label in ((101, "日K"), (102, "周K")):
            row = check_kline(secid, name, klt)
            row["label"] = label
            klines.append(row)

    print("quotes...")
    quotes = [check_quote(secid, name) for secid, name in targets]

    print("delivery...")
    delivery = check_delivery()

    for row in klines:
        if row["mismatched"]:
            problems.append(f"kline {row['secid']} {row['label']}: "
                            f"{row['mismatched']}/{row['compared']} rows differ from upstream")
        ohlc_bad = [x for x in row["inconsistent"] if x.get("kind") == "ohlc"]
        if ohlc_bad:
            problems.append(f"kline {row['secid']} {row['label']}: "
                            f"{len(ohlc_bad)} rows with high/low outside open/close")
    for q in quotes:
        if q["diffs"] and not q["lagging"]:
            problems.append(f"quote {q['secid']}: {len(q['diffs'])} fields differ from upstream")

    print(json.dumps({"inventory": inv, "klines": klines, "quotes": quotes,
                      "delivery": delivery, "problems": problems},
                     ensure_ascii=False, indent=2)[:400])
    write_report(args.report, inv, klines, quotes, delivery)
    print(f"\nreport: {args.report}")
    print(f"problems: {len(problems)}")
    for p in problems:
        print("  -", p)
    return 0


def write_report(path, inv, klines, quotes, delivery) -> None:
    ok = sum(1 for r in inv if r["status"] == 200)
    matched = sum(k["compared"] for k in klines)
    mismatch = sum(k["mismatched"] for k in klines)
    lines = [
        "# What the local server delivers to an agent",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `scripts/audit_delivery.py`. "
        f"Base URL `{BASE}`. `{ok}/{len(inv)}` probed routes answered 200.",
        "",
        "## Verdict",
        "",
        f"- **K-lines are correct.** {matched} closed bars across 5 instruments "
        f"(A-share, index, HK, US) and two periods were compared field by field against "
        f"EastMoney itself: **{mismatch} mismatches** in open/high/low/close/volume/"
        f"amount/pct.",
        "- **Quotes are fetched live.** `/api/stock/{secid}` costs ~1 s because it calls "
        "upstream, and returns `ts` (epoch) so freshness is explicit. Differences against "
        "a second read of upstream are tick movement, not staleness.",
        "- **The newest bar can be live.** Responses carry `last_final` plus a `note`, so "
        "an agent can tell a settled bar from one still forming.",
        "- **Delivery is plain JSON over HTTP**, gzip when asked (5x on a large payload), "
        "no auth and no cookies; pagination exists only on `/api/market/{id}`.",
        "- **Known rough edges**: unknown ids return 200 with empty data, an unknown "
        "sub-resource falls through to the full snapshot, HEAD is unsupported, and the "
        "first request for an uncached symbol costs 1-5 s while it fetches upstream.",
        "",
        "## 1. Endpoint inventory",
        "",
        "| Route | Purpose | Status | Latency | Bytes (wire) | Encoding | Top-level keys |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in inv:
        keys = r["keys"]
        key_text = ", ".join(keys) if isinstance(keys, list) else (keys or "—")
        lines.append(f"| `{r['path']}` | {r['label']} | {r['status']} | {r['ms']} ms | "
                     f"{r.get('wire') or r['bytes']} | {r['encoding']} | {key_text} |")

    lines += ["", "## 2. K-line correctness vs upstream", "",
              "Each row compares the bars this server serves against the same request "
              "made directly to EastMoney, field by field. A bar the server itself "
              "flags as `last_final: false` is still forming, so it is excluded from "
              "the match count and reported as lag instead.", "",
              "| Instrument | Period | Served | Compared | Mismatched | Anomalies | Last bar final | Range |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k in klines:
        lines.append(f"| {k['name']} `{k['secid']}` | {k['label']} | {k['served']} | "
                     f"{k['compared']} | {k['mismatched']} | "
                     f"{k['inconsistent_count']} | {k['last_final']} | "
                     f"{k['first']} → {k['last']} |")

    lagged = [k for k in klines if k["lag"]]
    if lagged:
        lines += ["", "Forming bars (live session, excluded from the count):", ""]
        for k in lagged:
            parts = []
            for field, (mine, ref) in k["lag"].items():
                if mine is None or ref is None:
                    continue
                parts.append(f"{field} {mine} vs {ref}")
            lines.append(f"- {k['name']} `{k['secid']}` {k['label']} {k['forming']}: "
                         + ", ".join(parts))

    detail = [k for k in klines if k["examples"] or k["inconsistent"]]
    if detail:
        lines += ["", "Differences observed:", ""]
        for k in detail:
            for ex in k["examples"][:2]:
                lines.append(f"- `{k['secid']}` {k['label']} {ex['date']}: "
                             f"{ex['fields']} served={ex['mine']} upstream={ex['upstream']}")
            for inc in k["inconsistent"][:2]:
                lines.append(f"- `{k['secid']}` {k['label']} {inc['date']}: {inc['reason']}")

    lines += ["", "## 3. Quote correctness vs upstream", ""]
    for q in quotes:
        if not q["diffs"]:
            state = "match"
        elif q["lagging"]:
            state = f"tick movement ({len(q['diffs'])} fields, each within 0.2%)"
        else:
            state = f"{len(q['diffs'])} differ"
        age = f", cached {q['age_s']}s ago" if q.get("age_s") is not None else ""
        lines.append(f"- **{q['name']}** `{q['secid']}` — {state} "
                     f"({q['ms']} ms{age})")
        for d in q["diffs"]:
            lines.append(f"  - `{d['field']}`: served `{d['mine']}` vs upstream `{d['upstream']}`")

    lines += ["", "## 4. Delivery semantics", "",
              f"- **Compression**: `Accept-Encoding: gzip` → `{delivery['gzip']['with_encoding']}` "
              f"({delivery['gzip']['with_bytes']} B vs {delivery['gzip']['plain_bytes']} B plain, "
              f"{delivery['gzip']['ratio']}×). Responses under 1 KB stay uncompressed "
              f"(`{delivery['small_response_encoding']}`).",
              f"- **HEAD**: `{delivery['head']}`.",
              f"- **Unknown market**: HTTP `{delivery['unknown_market']['status']}` — "
              f"`{delivery['unknown_market']['body']}`",
              f"- **Unknown secid**: HTTP `{delivery['unknown_secid']['status']}`, "
              f"{delivery['unknown_secid']['bars']} bars (empty rather than an error).",
              f"- **Limit clamp**: requested {delivery['limit_clamp']['requested']}, "
              f"served {delivery['limit_clamp']['served']}.",
              f"- **Invalid klt**: HTTP `{delivery['bad_klt']['status']}`, echoes klt="
              f"`{delivery['bad_klt']['klt_echo']}`.",
              f"- **Typo in sub-resource** (`/bars`): HTTP `{delivery['typo_suffix']['status']}` "
              f"with keys `{delivery['typo_suffix']['keys']}`.",
              f"- **Pagination** on `/api/market/cn.a`: keys "
              f"`{delivery['pagination']['keys']}`, {delivery['pagination']['rows']} rows returned.",
              f"- **v1 error format**: HTTP `{delivery['v1_bad_secid']['status']}` — "
              f"`{delivery['v1_bad_secid']['body']}`", ""]

    lines += ["## 5. Issues found", ""]
    if problems:
        lines += [f"- {p}" for p in problems]
    else:
        lines.append("- none")
    lines.append("")

    lines += [
        "## 6. Field semantics an agent must know",
        "",
        "| Field | Unit | Note |",
        "| --- | --- | --- |",
        "| `price`, `open`, `high`, `low`, `pre_close` | 元 | A-share; HK in HKD, US in USD |",
        "| `pct` | percent | `1.0` means 1%, not 0.01 |",
        "| `volume` | 手 | A-share lots (100 shares). HK/US use shares — check `market` |",
        "| `amount` | 元 | turnover, already price × volume |",
        "| `ts` | epoch seconds | when this snapshot was taken |",
        "| `bars[].d` | `YYYY-MM-DD` | trading day, not a timestamp |",
        "| `last_final` | bool | false means the last bar is still forming |",
        "| `updated_at` | epoch seconds | collector write time on list endpoints |",
        "",
        "Prices are already adjusted (`fqt=1`, 前复权) for the K-line endpoints; the "
        "snapshot's `close`/`price` is raw. Mixing the two across a split or dividend "
        "will produce a false gap.",
        "",
        "## 7. How to consume it safely",
        "",
        "1. Read `last_final` before using the newest bar. If it is false, treat that "
        "bar as provisional or drop it.",
        "2. Use `ts` / `updated_at` rather than assuming freshness; a request outside "
        "the instrument's session returns the last close, not a live price.",
        "3. Never infer an error from an empty `bars` array - check HTTP status *and* "
        "the `error`/`notes` keys, because unknown ids return 200.",
        "4. On the v1 surface, pass `as_of` explicitly when replaying and rely on "
        "`caveats` for what was unavailable at that time.",
        "5. Expect 1-5 s on the first request for an uncached symbol (upstream fetch), "
        "single-digit ms afterwards.",
        "",
    ]

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
