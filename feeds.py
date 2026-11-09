"""T+1 signal feeds scraped from EastMoney.

Everything here is chosen for next-session predictive value: who limited up and
why, who the hot money bought on the dragon-tiger list, where main capital is
rotating, how margin positions moved, what was announced, what analysts changed,
and what retail is piling into. Each feed returns normalised records; the
storage layer keeps them for seven days.

Record shape (all feeds):
    {"source": str, "day": "YYYY-MM-DD", "key": str, "name": str,
     "kind": "stock"|"sector"|"market", "score": float, "payload": dict}
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from em_client import EastMoneyClient, EastMoneyError, num

log = logging.getLogger("em.feeds")

CN_TZ = timezone(timedelta(hours=8))
PUSH2EX = "push2ex.eastmoney.com"
DATACENTER = "datacenter-web.eastmoney.com"
NOTICE = "np-anotice-stock.eastmoney.com"
REPORT = "reportapi.eastmoney.com"
RANK = "emappdata.eastmoney.com"
UT_POOL = "7eea3edcaed734bea9cbfc24409ed989"


@dataclass(frozen=True, slots=True)
class Feed:
    id: str
    name: str
    kind: str          # "intraday" | "daily"
    interval: int      # seconds, for intraday feeds
    description: str


def cn_day(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch or time.time(), CN_TZ).strftime("%Y-%m-%d")


def _secid(market, code) -> str:
    return f"{market}.{code}"


def _rec(source, day, key, name, kind, score, payload, stock=None) -> dict:
    if stock is None and kind == "stock" and key.count(".") == 1 and key.split(".")[0].isdigit():
        stock = key
    return {"source": source, "day": day, "key": str(key), "stock": stock, "name": name,
            "kind": kind, "score": float(score or 0), "payload": payload}


# --------------------------------------------------------------------------
# limit-up / limit-down pools (push2ex)
# --------------------------------------------------------------------------
def fetch_limit_pool(client: EastMoneyClient, day: str) -> list[dict]:
    out: list[dict] = []
    stamp = day.replace("-", "")
    for source, endpoint, sort in (("limit_up", "getTopicZTPool", "fbt:asc"),
                                   ("limit_down", "getTopicDTPool", "fund:asc")):
        params = {"ut": UT_POOL, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 200,
                  "sort": sort, "date": stamp, "_": int(time.time() * 1000)}
        try:
            body = client.get_url("/" + endpoint, params, host=PUSH2EX)
        except EastMoneyError as exc:
            log.warning("%s failed: %s", source, exc)
            continue
        try:
            data = json.loads(body).get("data") or {}
        except ValueError:
            continue
        pool_day = str(data.get("qdate") or stamp)
        pool_day = f"{pool_day[:4]}-{pool_day[4:6]}-{pool_day[6:8]}" if len(pool_day) == 8 else day
        for item in data.get("pool") or []:
            code, market = item.get("c"), item.get("m")
            if not code:
                continue
            price = num(item.get("p"))
            # the pool endpoint reports prices scaled by 1000
            if price and price > 10000:
                price = round(price / 1000, 3)
            payload = {
                "price": price,
                "pct": round(num(item.get("zdp")) or 0, 3),
                "amount": num(item.get("amount")),
                "float_mcap": num(item.get("ltsz")),
                "turnover": round(num(item.get("hs")) or 0, 3),
                "boards": item.get("lbc"),
                "first_seal": str(item.get("fbt") or "").zfill(6),
                "last_seal": str(item.get("lbt") or "").zfill(6),
                "seal_amount": num(item.get("fund")),
                "broken": item.get("zbc"),
                "industry": item.get("hybk"),
                "stat": item.get("zttj") or {},
                "market": market,
                "code": code,
            }
            score = num(item.get("fund")) or 0
            out.append(_rec(source, pool_day, _secid(market, code), item.get("n") or code,
                            "stock", score, payload))
    return out


def fetch_limit_up(client: EastMoneyClient, day: str) -> list[dict]:
    return [r for r in fetch_limit_pool(client, day) if r["source"] == "limit_up"]


def fetch_limit_down(client: EastMoneyClient, day: str) -> list[dict]:
    return [r for r in fetch_limit_pool(client, day) if r["source"] == "limit_down"]


# --------------------------------------------------------------------------
# dragon-tiger list (datacenter)
# --------------------------------------------------------------------------
def fetch_dragon_tiger(client: EastMoneyClient, day: str) -> list[dict]:
    params = {
        "sortColumns": "BILLBOARD_NET_AMT",
        "sortTypes": "-1",
        "pageSize": "100",
        "pageNumber": "1",
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLAIN,CLOSE_PRICE,"
                    "CHANGE_RATE,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,"
                    "ACCUM_AMOUNT,EXPLANATION"),
        "source": "WEB",
        "client": "WEB",
        "filter": f"(TRADE_DATE='{day}')",
    }
    body = client.get_url("/api/data/v1/get", params, host=DATACENTER)
    payload = json.loads(body)
    rows = ((payload.get("result") or {}).get("data")) or []
    out = []
    for row in rows:
        code = row.get("SECURITY_CODE")
        if not code:
            continue
        market = "1" if str(code).startswith(("6", "9")) else "0"
        out.append(_rec("dragon_tiger", day, _secid(market, code),
                        row.get("SECURITY_NAME_ABBR") or code, "stock",
                        num(row.get("BILLBOARD_NET_AMT")) or 0,
                        {"close": num(row.get("CLOSE_PRICE")),
                         "pct": num(row.get("CHANGE_RATE")),
                         "net_buy": num(row.get("BILLBOARD_NET_AMT")),
                         "buy": num(row.get("BILLBOARD_BUY_AMT")),
                         "sell": num(row.get("BILLBOARD_SELL_AMT")),
                         "amount": num(row.get("ACCUM_AMOUNT")),
                         "reason": row.get("EXPLANATION"),
                         "seats": row.get("EXPLAIN")}))
    return out


# --------------------------------------------------------------------------
# margin trading balances (datacenter)
# --------------------------------------------------------------------------
def _margin_rows(client: EastMoneyClient, day: str, sort: str) -> list[dict]:
    params = {
        "reportName": "RPTA_WEB_RZRQ_GGMX",
        "columns": "ALL",
        "filter": f"(DATE='{day}')",
        "pageNumber": "1",
        "pageSize": "50",
        "sortColumns": sort,
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    body = client.get_url("/api/data/v1/get", params, host=DATACENTER)
    payload = json.loads(body)
    return ((payload.get("result") or {}).get("data")) or []


def fetch_margin(client: EastMoneyClient, day: str) -> list[dict]:
    """Latest published margin detail; the exchange posts it after the close."""
    for candidate in (day, (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"),
                      (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d")):
        try:
            rows = _margin_rows(client, candidate, "RZYE")
        except EastMoneyError as exc:
            log.warning("margin %s failed: %s", candidate, exc)
            continue
        if not rows:
            continue
        try:
            net_rows = _margin_rows(client, candidate, "RZJME")
        except EastMoneyError:
            net_rows = []
        by_code = {r.get("SCODE"): r for r in rows}
        for row in net_rows:
            by_code.setdefault(row.get("SCODE"), row)
        out = []
        for code, row in by_code.items():
            if not code:
                continue
            market = "1" if str(code).startswith(("6", "9")) else "0"
            out.append(_rec("margin", candidate, _secid(market, code),
                            row.get("SECNAME") or code, "stock",
                            num(row.get("RZJME")) or 0,
                            {"balance": num(row.get("RZYE")),
                             "net_buy": num(row.get("RZJME")),
                             "net_buy_3d": num(row.get("RZJME3D")),
                             "net_buy_5d": num(row.get("RZJME5D")),
                             "buy": num(row.get("RZMRE")),
                             "repay": num(row.get("RZCHE")),
                             "short_balance": num(row.get("RQYE")),
                             "total_balance": num(row.get("RZRQYE")),
                             "balance_ratio": num(row.get("RZYEZB"))}))
        if out:
            return out
    return []


# --------------------------------------------------------------------------
# capital flow (push2 clist)
# --------------------------------------------------------------------------
CN_STOCKS_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def _flow_rows(client: EastMoneyClient, fs: str, fid: str, po: int, size: int) -> list[dict]:
    params = {"pn": 1, "pz": size, "po": po, "np": 1, "fltt": 2, "invt": 2,
              "fid": fid, "fs": fs,
              "fields": "f12,f13,f14,f2,f3,f62,f184,f66,f69,f72,f75,f8,f6",
              "_": int(time.time() * 1000)}
    body = client.get_url("/api/qt/clist/get", params, host="push2.eastmoney.com")
    payload = json.loads(body)
    return ((payload.get("data") or {}).get("diff")) or []


def fetch_stock_flow(client: EastMoneyClient, day: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for po in (1, 0):   # biggest inflow, then biggest outflow
        try:
            rows = _flow_rows(client, CN_STOCKS_FS, "f62", po, 60)
        except EastMoneyError as exc:
            log.warning("stock flow failed: %s", exc)
            continue
        for row in rows:
            code = row.get("f12")
            if not code or code in seen:
                continue
            seen.add(code)
            market = row.get("f13")
            out.append(_rec("stock_flow", day, _secid(market, code), row.get("f14") or code,
                            "stock", num(row.get("f62")) or 0,
                            {"price": num(row.get("f2")), "pct": num(row.get("f3")),
                             "main_net": num(row.get("f62")),
                             "main_ratio": num(row.get("f184")),
                             "super_net": num(row.get("f66")),
                             "big_net": num(row.get("f72")),
                             "turnover": num(row.get("f8")),
                             "amount": num(row.get("f6"))}))
    return out


def fetch_sector_flow(client: EastMoneyClient, day: str) -> list[dict]:
    out: list[dict] = []
    for fs, kind in (("m:90+t:2", "industry"), ("m:90+t:3", "concept")):
        try:
            rows = _flow_rows(client, fs, "f62", 1, 20)
        except EastMoneyError as exc:
            log.warning("sector flow %s failed: %s", kind, exc)
            continue
        for row in rows:
            code = row.get("f12")
            if not code:
                continue
            out.append(_rec("sector_flow", day, f"{kind}:{code}", row.get("f14") or code,
                            "sector", num(row.get("f62")) or 0,
                            {"kind": kind, "pct": num(row.get("f3")),
                             "main_net": num(row.get("f62")),
                             "main_ratio": num(row.get("f184")),
                             "super_net": num(row.get("f66")),
                             "big_net": num(row.get("f72")),
                             "amount": num(row.get("f6"))}))
    return out


# --------------------------------------------------------------------------
# northbound / southbound connect
# --------------------------------------------------------------------------
def fetch_connect(client: EastMoneyClient, day: str) -> list[dict]:
    params = {"fields1": "f1,f2,f3,f4",
              "fields2": "f51,f52,f53,f54,f56,f62,f63,f65,f66",
              "_": int(time.time() * 1000)}
    body = client.get_url("/api/qt/kamt/get", params, host="push2.eastmoney.com")
    data = json.loads(body).get("data") or {}
    labels = {"hk2sh": "沪股通", "hk2sz": "深股通", "sh2hk": "港股通(沪)", "sz2hk": "港股通(深)"}
    out = []
    for key, label in labels.items():
        row = data.get(key)
        if not row:
            continue
        out.append(_rec("connect", day, f"connect:{key}", label, "market",
                        num(row.get("buySellAmt")) or 0,
                        {"turnover": num(row.get("buySellAmt")),
                         "net_buy": num(row.get("netBuyAmt")),
                         "net_in": num(row.get("dayNetAmtIn")),
                         "remain": num(row.get("dayAmtRemain")),
                         "quota": num(row.get("dayAmtThreshold")),
                         "date": row.get("date2"), "status": row.get("status")}))
    return out


# --------------------------------------------------------------------------
# announcements
# --------------------------------------------------------------------------
def fetch_announcements(client: EastMoneyClient, day: str) -> list[dict]:
    params = {"page_size": 100, "page_index": 1, "ann_type": "A",
              "client_source": "web", "_": int(time.time() * 1000)}
    body = client.get_url("/api/security/ann", params, host=NOTICE)
    payload = json.loads(body)
    rows = ((payload.get("data") or {}).get("list")) or []
    out = []
    for row in rows:
        codes = row.get("codes") or []
        code = codes[0].get("stock_code") if codes else None
        market = codes[0].get("market_code") if codes else None
        if not code:
            continue
        title = (row.get("title") or "").strip()
        art = row.get("art_code") or f"{code}:{title[:20]}"
        out.append(_rec("announcement", day, f"ann:{art}",
                        codes[0].get("short_name") or code, "stock", 0,
                        {"title": title, "code": code,
                         "date": (row.get("notice_date") or "")[:10],
                         "display_time": (row.get("display_time") or "")[:19],
                         "art_code": art,
                         "url": f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"},
                        stock=_secid(market, code) if market else None))
    return out


# --------------------------------------------------------------------------
# broker research with forecasts
# --------------------------------------------------------------------------
def fetch_research(client: EastMoneyClient, day: str) -> list[dict]:
    begin = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    params = {"industryCode": "*", "pageSize": 100, "industry": "*", "rating": "",
              "ratingChange": "", "beginTime": begin, "endTime": day,
              "pageNo": 1, "fields": "", "qType": 0, "orgCode": "", "code": "*",
              "rcode": "", "p": 1, "pageNum": 1, "pageNumber": 1,
              "_": int(time.time() * 1000)}
    body = client.get_url("/report/list", params, host=REPORT)
    payload = json.loads(body)
    rows = payload.get("data") or []
    out = []
    for row in rows:
        code = row.get("stockCode")
        if not code:
            continue
        market = "1" if str(code).startswith(("6", "9")) else "0"
        out.append(_rec("research", day, f"rep:{row.get('infoCode')}",
                        row.get("stockName") or code, "stock", 0,
                        {"code": code,
                         "title": (row.get("title") or "").strip(),
                         "org": row.get("orgSName"),
                         "date": (row.get("publishDate") or "")[:10],
                         "eps_this": num(row.get("predictThisYearEps")),
                         "pe_this": num(row.get("predictThisYearPe")),
                         "eps_next": num(row.get("predictNextYearEps")),
                         "pe_next": num(row.get("predictNextYearPe")),
                         "rating": row.get("emRatingName") or row.get("sRatingName"),
                         "info_code": row.get("infoCode")},
                        stock=_secid(market, code)))
    return out


# --------------------------------------------------------------------------
# retail popularity ranking
# --------------------------------------------------------------------------
def fetch_popularity(client: EastMoneyClient, day: str) -> list[dict]:
    payload = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
               "marketType": "", "pageNo": 1, "pageSize": 100}
    body = client.post_json("/stockrank/getAllCurrentList", payload, host=RANK)
    data = json.loads(body).get("data") or []
    out = []
    for row in data:
        sc = row.get("sc") or ""       # e.g. SZ300308
        if len(sc) < 8:
            continue
        prefix, code = sc[:2], sc[2:]
        market = {"SH": "1", "SZ": "0", "BJ": "0"}.get(prefix, "0")
        rank = num(row.get("rk")) or 0
        out.append(_rec("popularity", day, _secid(market, code), code, "stock",
                        100 - rank, {"rank": int(rank), "prev_rank": num(row.get("hisRc")),
                                     "delta": num(row.get("rc")), "raw": sc}))
    return out


FEEDS: tuple[Feed, ...] = (
    Feed("limit_up", "涨停池", "intraday", 300, "今日涨停、连板、封单与题材"),
    Feed("limit_down", "跌停池", "intraday", 600, "今日跌停与封单"),
    Feed("stock_flow", "个股主力净流入", "intraday", 300, "主力净买入排行（含流出）"),
    Feed("sector_flow", "板块资金流", "intraday", 600, "行业与概念主力净流入"),
    Feed("connect", "沪深港通", "intraday", 600, "北向/南向成交与净买入"),
    Feed("dragon_tiger", "龙虎榜", "daily", 0, "席位净买入与上榜原因"),
    Feed("margin", "融资融券", "daily", 0, "融资余额与净买入变化"),
    Feed("announcement", "公告", "intraday", 1800, "最新公告"),
    Feed("research", "研报", "daily", 0, "机构评级与盈利预测"),
    Feed("popularity", "人气榜", "intraday", 900, "股吧人气排名与变化"),
)

FETCHERS: dict[str, Callable[[EastMoneyClient, str], list[dict]]] = {
    "limit_up": fetch_limit_up,
    "limit_down": fetch_limit_down,
    "stock_flow": fetch_stock_flow,
    "sector_flow": fetch_sector_flow,
    "connect": fetch_connect,
    "dragon_tiger": fetch_dragon_tiger,
    "margin": fetch_margin,
    "announcement": fetch_announcements,
    "research": fetch_research,
    "popularity": fetch_popularity,
}
