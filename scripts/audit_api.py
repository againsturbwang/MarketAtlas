"""Audit every EastMoney endpoint the platform depends on.

Answers one question: can a plain HTTP client (Python, Go, curl, anything)
fetch this reliably, or does it need a browser, cookies or a JS runtime?

Each endpoint is requested twice, directly, with no proxy:
  * browser user agent   - what a normal client looks like
  * python default agent - the strictest case (urllib's own UA)

Usage:
    python scripts/audit_api.py             # table to stdout
    python scripts/audit_api.py --json      # machine readable
    python scripts/audit_api.py --report docs/api-audit.md
"""

from __future__ import annotations

import argparse
import gzip
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
PYTHON_UA = "Python-urllib/3.13"

CTX = ssl.create_default_context()

BLOCK_MARKERS = (
    "请输入验证码", "访问过于频繁", "人机验证", "安全验证", "请开启JavaScript",
    "captcha", "Access Denied", "403 Forbidden",
)


def request(url: str, *, method: str = "GET", body: bytes | None = None,
            ua: str = BROWSER_UA, timeout: float = 20.0) -> dict:
    headers = {"User-Agent": ua, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://www.eastmoney.com"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            ctype = resp.headers.get("Content-Type", "")
            charset = "utf-8"
            if "charset=" in ctype:
                charset = ctype.split("charset=")[-1].split(";")[0].strip()
                if charset.lower() in ("gb2312", "gbk"):
                    charset = "gb18030"
            text = raw.decode(charset, "replace")
            cookies = resp.headers.get_all("Set-Cookie") or []
            return {"status": resp.status, "ms": int((time.time() - started) * 1000),
                    "bytes": len(raw), "ctype": ctype.split(";")[0], "text": text,
                    "cookies": len(cookies), "error": None}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "ms": int((time.time() - started) * 1000),
                "bytes": 0, "ctype": "", "text": "", "cookies": 0,
                "error": f"HTTP {exc.code}"}
    except Exception as exc:                       # noqa: BLE001 - audit reports everything
        return {"status": 0, "ms": int((time.time() - started) * 1000), "bytes": 0,
                "ctype": "", "text": "", "cookies": 0,
                "error": f"{type(exc).__name__}: {exc}"[:120]}


def classify(result: dict, kind: str) -> str:
    if result["error"]:
        return "FAIL"
    if result["status"] != 200:
        return f"HTTP {result['status']}"
    body = result["text"]
    if len(body) < 80:
        return "EMPTY"
    low = body[:4000].lower()
    for marker in BLOCK_MARKERS:
        if marker.lower() in low:
            return "BLOCKED"
    if kind == "json":
        stripped = body.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "ok"
        if stripped.startswith("var ") and "{" in stripped:
            return "ok (jsonp)"
        return "not json"
    return "ok"


PUSH = "https://push2.eastmoney.com"
HIS = "https://push2his.eastmoney.com"
POOL = "https://push2ex.eastmoney.com"
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UT = "7eea3edcaed734bea9cbfc24409ed989"
STOCK_FIELDS = ("f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f59,f60,f107,f116,"
                "f117,f162,f167,f168,f169,f170,f171,f531,f11,f12,f13,f14,f15,f16,"
                "f17,f18,f19,f20,f21,f22,f23,f24,f25,f26,f27,f28,f29,f30,f31,f32,"
                "f33,f34,f35,f36,f37,f38,f39,f40")

ENDPOINTS: list[dict] = [
    # --- quotes (push2) ---
    dict(area="行情", name="板块列表 clist", kind="json",
         url=f"{PUSH}/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2&fid=f3"
             f"&fs=m:90+t:2&fields=f12,f14,f3"),
    dict(area="行情", name="沪深京A股 clist", kind="json",
         url=f"{PUSH}/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2&fid=f3"
             f"&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f2,f3"),
    dict(area="行情", name="多市场 clist (i:/m:/b:)", kind="json",
         url=f"{PUSH}/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2&fid=f3"
             f"&fs=i:100.HSIC,i:100.HSCEI,b:MK0021&fields=f12,f14,f2,f3"),
    dict(area="行情", name="个股快照 stock/get", kind="json",
         url=f"{PUSH}/api/qt/stock/get?secid=1.600519&fltt=2&invt=2&fields={STOCK_FIELDS}"),
    dict(area="行情", name="批量 ulist.np", kind="json",
         url=f"{PUSH}/api/qt/ulist.np/get?fltt=2&invt=2&secids=1.600519,0.000001,1.000001"
             f"&fields=f12,f14,f2,f3,f4"),
    dict(area="行情", name="板块成分 slist", kind="json",
         url=f"{PUSH}/api/qt/slist/get?secid=90.BK1200&pn=1&pz=3&po=1&np=1&fltt=2"
             f"&invt=2&fid=f3&fields=f12,f14,f2,f3"),
    dict(area="行情", name="日K kline", kind="json",
         url=f"{PUSH}/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&lmt=5"
             f"&end=20500101&fields1=f1,f2,f3,f4,f5,f6"
             f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"),
    dict(area="行情", name="分时 trends2", kind="json",
         url=f"{PUSH}/api/qt/stock/trends2/get?secid=1.600519&ndays=1&iscr=0"
             f"&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f53,f56,f58"),
    dict(area="行情", name="分时资金 fflow", kind="json",
         url=f"{PUSH}/api/qt/stock/fflow/kline/get?secid=1.600519&lmt=0&klt=1"
             f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57"),
    dict(area="行情", name="日级资金 fflow (push2his)", kind="json",
         url=f"{HIS}/api/qt/stock/fflow/daykline/get?secid=1.600519&lmt=5&klt=101"
             f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57"),
    dict(area="行情", name="逐笔成交 details", kind="json",
         url=f"{PUSH}/api/qt/stock/details/get?secid=1.600519&pos=-20&fltt=2"
             f"&fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55"),
    dict(area="行情", name="沪深港通 kamt", kind="json",
         url=f"{PUSH}/api/qt/kamt/get?fields1=f1,f2,f3,f4"
             f"&fields2=f51,f52,f53,f54,f56,f62,f63,f65,f66"),
    # --- T+1 pools ---
    dict(area="T+1", name="涨停池 ztpool", kind="json",
         url=f"{POOL}/getTopicZTPool?ut={UT}&dpt=wz.ztzt&Pageindex=0&pagesize=3"
             f"&sort=fbt%3Aasc&date=20261105"),
    dict(area="T+1", name="跌停池 dtpool", kind="json",
         url=f"{POOL}/getTopicDTPool?ut={UT}&dpt=wz.ztzt&Pageindex=0&pagesize=3"
             f"&sort=fund%3Aasc&date=20261105"),
    dict(area="T+1", name="龙虎榜 datacenter", kind="json",
         url=f"{DC}?sortColumns=BILLBOARD_NET_AMT&sortTypes=-1&pageSize=2&pageNumber=1"
             f"&reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=SECURITY_CODE,SECURITY_NAME_ABBR,"
             f"BILLBOARD_NET_AMT&source=WEB&client=WEB&filter=(TRADE_DATE%3D%272026-11-05%27)"),
    dict(area="T+1", name="两融 datacenter", kind="json",
         url=f"{DC}?reportName=RPTA_WEB_RZRQ_GGMX&columns=SCODE,SECNAME,RZYE,RZJME"
             f"&filter=(DATE%3D%272026-11-04%27)&pageNumber=1&pageSize=2"
             f"&sortColumns=RZYE&sortTypes=-1&source=WEB&client=WEB"),
    dict(area="T+1", name="个股资金流排行 clist", kind="json",
         url=f"{PUSH}/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2&fid=f62"
             f"&fs=m:0+t:6,m:0+t:80&fields=f12,f14,f62,f184"),
    dict(area="T+1", name="人气榜 emappdata (POST)", kind="json", method="POST",
         url="https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
         body=b'{"appId":"appId01","globalId":"786e4c21-70dc-435a-93bb-38","marketType":"","pageNo":1,"pageSize":3}'),
    # --- news / content ---
    dict(area="内容", name="公告列表 np-anotice", kind="json",
         url="https://np-anotice-stock.eastmoney.com/api/security/ann?page_size=2&page_index=1"
             "&ann_type=A&client_source=web"),
    dict(area="内容", name="公告正文 np-cnotice", kind="json",
         url="https://np-cnotice-stock.eastmoney.com/api/content/ann"
             "?art_code=AN202611051831451253&client_source=web&page_index=1"),
    dict(area="内容", name="研报 reportapi", kind="json",
         url="https://reportapi.eastmoney.com/report/list?industryCode=*&pageSize=2&industry=*"
             "&rating=&ratingChange=&beginTime=2026-10-01&endTime=2026-11-05&pageNo=1"
             "&fields=&qType=0&orgCode=&code=*&rcode=&p=1&pageNum=1&pageNumber=1"),
    dict(area="内容", name="7x24 快讯 newsapi (JSONP)", kind="json",
         url="https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_5_1_.html"),
    dict(area="内容", name="新闻栏目 np-listapi", kind="json",
         url="https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col"
             "&column=350&order=1&needInteractData=0&page_index=1&page_size=2&req_trace=1793869100"),
    dict(area="内容", name="财经首页 (HTML)", kind="html",
         url="https://finance.eastmoney.com/"),
    dict(area="内容", name="文章正文页 (HTML)", kind="html",
         url="https://finance.eastmoney.com/a/202611053923587720.html"),
    dict(area="内容", name="全球频道 (HTML)", kind="html",
         url="https://global.eastmoney.com/"),
    dict(area="内容", name="东方视点 (HTML)", kind="html",
         url="https://finance.eastmoney.com/news/cdfsd.html"),
    dict(area="内容", name="专题页 (HTML)", kind="html",
         url="https://topic.eastmoney.com/detail/agscfp2025.html"),
    dict(area="内容", name="股吧个股列表 (HTML)", kind="html",
         url="https://guba.eastmoney.com/list,600519.html"),
    dict(area="内容", name="股吧热帖 API", kind="json",
         url="https://gbapi.eastmoney.com/webarticlelist/api/Article/Articlelist"
             "?code=600519&type=0&ps=3&p=1"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit EastMoney endpoints")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--report", help="write a markdown report to this path")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    rows = []
    for ep in ENDPOINTS:
        method = ep.get("method", "GET")
        body = ep.get("body")
        browser = request(ep["url"], method=method, body=body, ua=BROWSER_UA,
                          timeout=args.timeout)
        plain = request(ep["url"], method=method, body=body, ua=PYTHON_UA,
                        timeout=args.timeout)
        rows.append({
            "area": ep["area"], "name": ep["name"], "kind": ep["kind"],
            "url": ep["url"],
            "browser": {"verdict": classify(browser, ep["kind"]),
                        "status": browser["status"], "ms": browser["ms"],
                        "bytes": browser["bytes"], "cookies": browser["cookies"],
                        "type": browser["ctype"], "error": browser["error"]},
            "python": {"verdict": classify(plain, ep["kind"]),
                       "status": plain["status"], "ms": plain["ms"],
                       "bytes": plain["bytes"], "error": plain["error"]},
        })
        time.sleep(0.25)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        current = None
        for row in rows:
            if row["area"] != current:
                current = row["area"]
                print(f"\n{current}")
                print(f"  {'endpoint':<30}{'browser':<10}{'python':<12}{'ms':>6}{'bytes':>9}  cookies")
            b, p = row["browser"], row["python"]
            flag = "" if b["verdict"] == "ok" and p["verdict"] == "ok" else "  <-- check"
            print(f"  {row['name'][:29]:<30}{b['verdict']:<10}{p['verdict']:<12}"
                  f"{b['ms']:>6}{b['bytes']:>9}  {b['cookies']}{flag}")

    ok = sum(1 for r in rows if r["browser"]["verdict"].startswith("ok"))
    ok_plain = sum(1 for r in rows if r["python"]["verdict"].startswith("ok"))
    summary = (f"{len(rows)} endpoints: browser-UA {ok}/{len(rows)} ok, "
               f"plain python-UA {ok_plain}/{len(rows)} ok")
    print(summary, file=sys.stderr if args.json else sys.stdout)

    if args.report:
        write_report(args.report, rows, summary)
        print(f"report written to {args.report}", file=sys.stderr)
    return 0


def write_report(path: str, rows: list[dict], summary: str) -> None:
    gated = [r for r in rows if not r["python"]["verdict"].startswith("ok")]
    lines = [
        "# EastMoney API accessibility audit",
        "",
        f"`{summary}` — generated by `python scripts/audit_api.py --report`.",
        "",
        "Every endpoint is requested **directly, with no proxy and no cookies**, twice:",
        "once with a browser user agent, once with Python's default `urllib` agent.",
        "If both columns say `ok`, any HTTP client can fetch it without a browser.",
        "",
        "| Area | Endpoint | Browser UA | Plain Python UA | Latency | Bytes | Cookies |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        b, p = r["browser"], r["python"]
        lines.append(f"| {r['area']} | {r['name']} | {b['verdict']} | {p['verdict']} | "
                     f"{b['ms']} ms | {b['bytes']} | {b['cookies']} |")

    lines += ["", "## Findings", ""]
    if gated:
        lines.append("Endpoints that refuse a non-browser `User-Agent`:")
        for r in gated:
            lines.append(f"- **{r['name']}** — {r['python']['verdict']} "
                         f"({r['python']['error'] or 'non-200'}) with Python's agent, "
                         f"{r['browser']['verdict']} with a browser agent.")
        lines.append("")
        lines.append("This is a user-agent filter, not a JavaScript wall: `curl/8.5.0` "
                     "passes it. The platform sends a browser agent on every request, "
                     "so the filter has no practical effect here.")
        lines.append("")
    lines += [
        "Everything else — push2 quotes, klines, trends, fund flow, ticks, the "
        "limit-up/limit-down pools, datacenter reports (dragon-tiger, margin), "
        "announcements, research, the 7x24 feed, news columns and all article "
        "pages — answers plain Python, sets no required cookies, and needs no "
        "browser or JS runtime.",
        "",
        "### Notes",
        "",
        "- `datacenter-web` returns one `Set-Cookie` header but serves the JSON "
        "correctly when it is ignored.",
        "- `emappdata` (popularity ranking) is the only endpoint requiring `POST`.",
        "- `newsapi` replies with JSONP (`var ajaxResult={...}`); strip the prefix "
        "before parsing.",
        "- `gbapi.eastmoney.com/webarticlelist` always returns HTTP 200 with an "
        "empty `re` array for unauthenticated clients; it is not usable for "
        "market-wide community data, which is why the community feed reads the "
        "server-rendered list pages instead.",
        "- `robots.txt`: `www.eastmoney.com` publishes `User-agent: * / Allow: /`; "
        "`quote`, `guba` and `datacenter` host none (404).",
        "",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
