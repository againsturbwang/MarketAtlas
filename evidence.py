"""Evidence packets for an LLM agent doing T+1 prediction.

The contract in one sentence: every query is point-in-time (`as_of`), every
number carries its unit, every fact carries a source, and anything that was
not yet published at `as_of` is absent rather than silently substituted.

That last rule is what makes replay honest. Ask for `as_of=2026-11-04T15:30`
and you get exactly what a decision-maker could have known at that moment -
no evening dragon-tiger list, no next-day news.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA = "em.evidence/1"
CN_TZ = timezone(timedelta(hours=8))
CLOSE_HOUR, CLOSE_MINUTE = 15, 0

UNITS = {
    "amount": "元 (CNY)",
    "pct": "percent, 1.0 = 1%",
    "price": "元",
    "volume": "手",
    "color": "red = up, green = down",
    "flow": "主力净额 = super + large order net, 元",
}

GLOSSARY = {
    "f62": "主力净流入额 (元)",
    "f184": "主力净占比 (%)",
    "lbc": "连板数",
    "fund": "涨停/跌停封单额 (元)",
    "zbc": "炸板次数",
    "RZYE": "融资余额 (元)",
    "RZJME": "融资净买入 (元)",
    "secid": "market.code, 1=SH 0=SZ",
}


@dataclass(frozen=True, slots=True)
class AsOf:
    iso: str
    ts: int
    date: str
    is_latest: bool


def resolve_as_of(text: str | None, now: float | None = None) -> AsOf:
    """Default the cutoff to the last session close, never to "now"."""
    now = now if now is not None else time.time()
    current = datetime.fromtimestamp(now, CN_TZ)
    if text:
        raw = text.strip().replace("Z", "+00:00")
        try:
            if len(raw) == 10:
                moment = datetime.strptime(raw, "%Y-%m-%d").replace(
                    hour=CLOSE_HOUR, minute=CLOSE_MINUTE, tzinfo=CN_TZ)
            else:
                moment = datetime.fromisoformat(raw)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=CN_TZ)
                else:
                    moment = moment.astimezone(CN_TZ)
        except ValueError as exc:
            raise ValueError(f"as_of must be YYYY-MM-DD or ISO datetime: {exc}") from exc
        return AsOf(moment.isoformat(), int(moment.timestamp()),
                    moment.strftime("%Y-%m-%d"), False)

    # Default: look at everything published so far, but anchor the session to the
    # last close. Asking in the evening therefore sees the dragon-tiger list that
    # appeared at 17:30, while a replay cut at 15:00 does not.
    trade_date = current
    if current.hour * 60 + current.minute < CLOSE_HOUR * 60 + CLOSE_MINUTE:
        trade_date = current - timedelta(days=1)
    while trade_date.weekday() >= 5:       # weekends are not sessions
        trade_date -= timedelta(days=1)
    return AsOf(current.isoformat(), int(current.timestamp()),
                trade_date.strftime("%Y-%m-%d"), True)


def _packet(kind: str, as_of: AsOf, **body) -> dict:
    packet = {
        "schema": SCHEMA,
        "kind": kind,
        "as_of": as_of.iso,
        "trade_date": as_of.date,
        "generated_at": datetime.fromtimestamp(time.time(), CN_TZ).isoformat(),
        "units": UNITS,
    }
    packet.update(body)
    return packet


def _source(source_id: str, label: str, url: str = "", ts: int | None = None) -> dict:
    return {"id": source_id, "label": label, "url": url, "ts": ts}


# When each feed becomes public knowledge on its trade day (CN local hour).
# `ts` on a signal row is when we collected it, which is later than publication
# for the evening feeds - filtering on it would hide the limit-up pool from a
# close-of-day cut and leak nothing useful in return.
PUBLISH_HOUR = {
    "limit_up": 15.0, "limit_down": 15.0, "stock_flow": 15.0, "sector_flow": 15.0,
    "connect": 15.0, "popularity": 15.0, "dragon_tiger": 17.5,
    "margin": 18.0, "research": 18.0,
}


def signal_published(row: dict) -> int:
    """Epoch at which this signal was public, not when we happened to store it."""
    hour = PUBLISH_HOUR.get(row.get("source", ""))
    day = row.get("day")
    if hour is None or not day:
        return int(row.get("ts") or 0)
    try:
        base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    except ValueError:
        return int(row.get("ts") or 0)
    return int((base + timedelta(hours=hour)).timestamp())


def _visible(rows: list[dict], as_of: AsOf) -> list[dict]:
    return [r for r in rows if signal_published(r) <= as_of.ts]


# --------------------------------------------------------------------------
# regime
# --------------------------------------------------------------------------
def build_regime(db, as_of: AsOf, headline_secids: list[tuple[str, str]]) -> dict:
    facts: list[dict] = []
    caveats: list[str] = []
    sources = [_source("kline", "东方财富日K"), _source("signals", "T+1 信号")]

    bars = db.kline_last_many([s for s, _ in headline_secids], as_of.date)
    indices = []
    for secid, name in headline_secids:
        bar = bars.get(secid)
        live = db.quote(secid)
        if bar:
            indices.append({"secid": secid, "name": name, "close": bar["close"],
                            "pct": bar["pct"], "date": bar["d"], "source": "kline"})
        elif live and as_of.is_latest:
            indices.append({"secid": secid, "name": name, "close": live["price"],
                            "pct": live["pct"], "date": as_of.date, "source": "quote_latest"})
    if any(i["source"] == "quote_latest" for i in indices):
        caveats.append("部分指数缺少该日K线，使用最新快照代替")
    facts.append({"kind": "indices", "items": indices, "source": "kline"})

    breadth = db.stats_at("cn.a", as_of.ts)
    if breadth:
        total = breadth.get("total") or 0
        up, down = breadth.get("up") or 0, breadth.get("down") or 0
        facts.append({"kind": "breadth", "market": "cn.a",
                      "total": total, "up": up, "down": down,
                      "flat": breadth.get("flat") or 0,
                      "limit_up": breadth.get("limit_up") or 0,
                      "limit_down": breadth.get("limit_down") or 0,
                      "limit_up_note": "按涨跌幅阈值统计，与涨停池口径不同",
                      "avg_pct": breadth.get("avg_pct"),
                      "amount": breadth.get("amount"),
                      "advance_ratio": round(up / total, 3) if total else None,
                      "source": "market_stats", "ts": breadth.get("ts")})
    else:
        caveats.append("cn.a 广度快照在 as_of 之前不可用")

    sector_rows = db.signals("sector_flow", as_of.date, 200, "desc")
    sector_rows = _visible(sector_rows, as_of)
    if sector_rows:
        facts.append({"kind": "sector_flow_top", "items": [
            {"name": r["name"], "main_net": r["payload"].get("main_net"),
             "pct": r["payload"].get("pct"), "kind": r["payload"].get("kind")}
            for r in sector_rows[:6]], "source": "signals:sector_flow"})
        facts.append({"kind": "sector_flow_bottom", "items": [
            {"name": r["name"], "main_net": r["payload"].get("main_net"),
             "pct": r["payload"].get("pct"), "kind": r["payload"].get("kind")}
            for r in sector_rows[-4:]], "source": "signals:sector_flow"})

    for source in ("limit_up", "limit_down"):
        rows = _visible(db.signals(source, as_of.date, 500, "desc"), as_of)
        if source == "limit_up":
            boards = [r["payload"].get("boards") or 1 for r in rows]
            facts.append({"kind": "limit_up_pool", "count": len(rows),
                          "max_boards": max(boards) if boards else 0,
                          "multi_board": sum(1 for b in boards if b >= 2),
                          "leaders": [{"name": r["name"], "secid": r["stock"],
                                       "boards": r["payload"].get("boards"),
                                       "seal_amount": r["payload"].get("seal_amount"),
                                       "industry": r["payload"].get("industry")}
                                      for r in rows[:8]],
                          "source": "signals:limit_up"})
        else:
            facts.append({"kind": "limit_down_pool", "count": len(rows),
                          "names": [r["name"] for r in rows[:5]],
                          "source": "signals:limit_down"})

    connect = _visible(db.signals("connect", as_of.date, 20, "desc"), as_of)
    if connect:
        facts.append({"kind": "connect", "items": [
            {"channel": r["name"], "turnover": r["payload"].get("turnover"),
             "net_buy": r["payload"].get("net_buy")} for r in connect],
            "source": "signals:connect"})
        if all(abs((r["payload"].get("net_buy") or 0)) < 1e4 for r in connect):
            caveats.append("北向资金自 2024-08 起不再披露盘中净买入，仅有成交额")

    margin = _visible(db.signals("margin", None, 500, "desc"), as_of)
    if margin:
        margin_day = margin[0]["day"]
        net = [r["payload"].get("net_buy") or 0 for r in margin]
        facts.append({"kind": "margin", "day": margin_day, "names": len(margin),
                      "net_buy_sum": sum(net),
                      "top": [{"name": r["name"], "net_buy": r["payload"].get("net_buy"),
                               "balance": r["payload"].get("balance")} for r in margin[:5]],
                      "source": "signals:margin"})
        if margin_day < as_of.date:
            caveats.append(f"融资融券数据截至 {margin_day}（交易所收盘后发布）")

    news = db.articles(days=2, limit=400)
    recent = [n for n in news if (n.get("published") or 0) <= as_of.ts]
    if recent:
        by_section: dict[str, int] = {}
        for item in recent:
            by_section[item["section"]] = by_section.get(item["section"], 0) + 1
        facts.append({"kind": "news_volume", "window_hours": 24,
                      "by_section": by_section, "source": "articles"})

    return _packet("regime", as_of, facts=facts, caveats=caveats, sources=sources)


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------
WEIGHTS = {
    "limit_up": 26.0, "dragon_tiger": 20.0, "main_flow": 18.0,
    "margin": 12.0, "popularity": 10.0, "research": 6.0,
}


def build_candidates(db, as_of: AsOf, limit: int = 20) -> dict:
    rows = []
    for source in ("limit_up", "dragon_tiger", "stock_flow", "margin", "popularity",
                   "research", "announcement"):
        for row in _visible(db.signals(source, as_of.date, 300, "desc"), as_of):
            if row.get("stock"):
                rows.append(row)

    by_stock: dict[str, dict] = {}
    for row in rows:
        slot = by_stock.setdefault(row["stock"], {"secid": row["stock"], "name": row["name"],
                                                  "signals": {}, "reasons": []})
        slot["signals"].setdefault(row["source"], []).append(row)

    def scale(value: float, ceiling: float) -> float:
        if not value:
            return 0.0
        return max(-1.0, min(1.0, value / ceiling))

    scored = []
    for secid, slot in by_stock.items():
        score = 0.0
        reasons = []
        sig = slot["signals"]

        if "limit_up" in sig:
            payload = sig["limit_up"][0]["payload"]
            boards = payload.get("boards") or 1
            seal = payload.get("seal_amount") or 0
            points = WEIGHTS["limit_up"] + min(12.0, boards * 4.0) + 8 * scale(seal, 5e8)
            score += points
            reasons.append({"signal": "limit_up", "value": f"{boards}板",
                            "detail": f"封单 {seal / 1e8:.2f}亿", "weight": round(points, 1)})
        if "dragon_tiger" in sig:
            payload = sig["dragon_tiger"][0]["payload"]
            net = payload.get("net_buy") or 0
            points = WEIGHTS["dragon_tiger"] * scale(net, 3e8)
            score += points
            reasons.append({"signal": "dragon_tiger", "value": f"{net / 1e8:+.2f}亿",
                            "detail": payload.get("reason") or "", "weight": round(points, 1)})
        if "stock_flow" in sig:
            payload = sig["stock_flow"][0]["payload"]
            ratio = payload.get("main_ratio") or 0
            points = WEIGHTS["main_flow"] * scale(ratio, 25.0)
            score += points
            reasons.append({"signal": "main_flow", "value": f"{ratio:+.2f}%",
                            "detail": f"净额 {(payload.get('main_net') or 0) / 1e8:.2f}亿",
                            "weight": round(points, 1)})
        if "margin" in sig:
            payload = sig["margin"][0]["payload"]
            net = payload.get("net_buy") or 0
            points = WEIGHTS["margin"] * scale(net, 1e8)
            score += points
            reasons.append({"signal": "margin", "value": f"{net / 1e8:+.2f}亿",
                            "weight": round(points, 1)})
        if "popularity" in sig:
            payload = sig["popularity"][0]["payload"]
            rank = payload.get("rank") or 100
            prev = payload.get("prev_rank") or rank
            jump = (prev - rank) if prev else 0
            points = WEIGHTS["popularity"] * (0.4 + 0.6 * scale(100 - rank, 100))
            if jump > 0:
                points += 4 * scale(jump, 50)
            score += points
            reasons.append({"signal": "popularity", "value": f"#{rank}",
                            "detail": f"前值 {prev}", "weight": round(points, 1)})
        if "research" in sig:
            payload = sig["research"][0]["payload"]
            score += WEIGHTS["research"] * 0.5
            reasons.append({"signal": "research", "value": payload.get("rating") or "研报",
                            "detail": (payload.get("title") or "")[:40],
                            "weight": round(WEIGHTS["research"] * 0.5, 1)})

        announcements = slot["signals"].get("announcement", [])
        scored.append({
            "secid": secid, "name": slot["name"],
            "score": round(score, 1),
            "signals": sorted(slot["signals"].keys()),
            "reasons": reasons,
            "announcements": [a["payload"].get("title", "")[:60] for a in announcements[:2]],
        })

    scored.sort(key=lambda s: (-s["score"], s["secid"]))
    top = scored[:limit]
    return _packet("candidates", as_of, count=len(top), total_scored=len(scored),
                   candidates=top,
                   method="score = Σ signal weights (limit_up 26 + 连板/封单, 龙虎榜 20, "
                          "主力占比 18, 两融 12, 人气 10, 研报 6)",
                   caveats=["排序是信号合流度，不是收益预测"],
                   sources=[_source("signals", "T+1 信号")])


# --------------------------------------------------------------------------
# per-stock evidence
# --------------------------------------------------------------------------
def build_stock(db, secid: str, as_of: AsOf, name: str | None = None) -> dict:
    quote = db.quote(secid)
    bars = db.kline_recent(secid, as_of.date, 25)
    facts: list[dict] = []
    caveats: list[str] = []
    sources = [_source("kline", "东方财富日K", ts=None)]

    if not name and quote:
        name = quote.get("name")
    label = name or secid

    if bars:
        last = bars[-1]
        closes = [b["close"] for b in bars if b["close"]]
        facts.append({
            "kind": "price", "date": last["d"], "close": last["close"],
            "pct": last["pct"], "volume": last["volume"], "amount": last["amount"],
            "source": "kline",
        })
        if len(closes) >= 6:
            facts.append({
                "kind": "momentum",
                "ret_1d": round((closes[-1] / closes[-2] - 1) * 100, 2),
                "ret_5d": round((closes[-1] / closes[-6] - 1) * 100, 2),
                "ret_20d": round((closes[-1] / closes[0] - 1) * 100, 2) if len(closes) >= 20 else None,
                "high_20d": max(closes), "low_20d": min(closes),
                "from_high_pct": round((closes[-1] / max(closes) - 1) * 100, 2),
                "source": "kline",
            })
    elif quote and as_of.is_latest:
        facts.append({"kind": "price", "date": as_of.date, "close": quote["price"],
                      "pct": quote["pct"], "volume": quote["volume"],
                      "amount": quote["amount"], "source": "quote_latest"})
        caveats.append("缺少日K，使用最新快照（可能包含 as_of 之后的成交）")
    else:
        caveats.append("as_of 之前没有该品种的行情")

    signals = _visible(db.signals_for(secid, 60), as_of)
    grouped: dict[str, list] = {}
    for row in signals:
        grouped.setdefault(row["source"], []).append(row)
    for source, items in grouped.items():
        facts.append({"kind": f"signal:{source}", "items": [
            {"day": r["day"], "name": r["name"], "score": r["score"], "payload": r["payload"]}
            for r in items[:4]], "source": f"signals:{source}"})

    hits = db.articles(days=7, limit=300)
    tokens = [label]
    if quote and quote.get("code"):
        tokens.append(quote["code"])
    related = []
    for item in hits:
        if (item.get("published") or 0) > as_of.ts:
            continue
        blobs = (item.get("title") or "") + (item.get("summary") or "")
        if any(t and t in blobs for t in tokens):
            related.append({"art_id": item["art_id"], "section": item["section"],
                            "title": item["title"], "published": item["published"],
                            "chars": item.get("words") or 0})
    if related:
        facts.append({"kind": "news", "matches": len(related),
                      "items": related[:6], "source": "articles",
                      "method": "标题/摘要包含名称或代码"})
    else:
        caveats.append("7日内容库中没有命中该品种的新闻（按名称/代码匹配标题与摘要）")

    if "margin" in grouped:
        latest = grouped["margin"][0]
        if latest["day"] < as_of.date:
            caveats.append(f"融资融券截至 {latest['day']}")
    if any(r["source"] == "stock_flow" and (r["payload"].get("main_ratio") or 0) == 0 for r in signals):
        caveats.append("资金流字段可能因市场口径不同而缺失")

    return _packet("stock_evidence", as_of, secid=secid, name=label,
                   facts=facts, caveats=caveats, sources=sources)


# --------------------------------------------------------------------------
# news search
# --------------------------------------------------------------------------
def build_news(db, as_of: AsOf, query: str | None, days: int = 7, limit: int = 20) -> dict:
    rows = db.articles(days=days, limit=400, q=query or None)
    items = [r for r in rows if (r.get("published") or 0) <= as_of.ts][:limit]
    return _packet("news", as_of, query=query or "*", count=len(items),
                   items=[{"art_id": r["art_id"], "section": r["section"],
                           "title": r["title"], "published": r["published"],
                           "media": r.get("media"), "chars": r.get("words") or 0,
                           "excerpt": (r.get("summary") or "")[:180],
                           "url": r.get("url")} for r in items],
                   caveats=[] if items else ["该时间窗内没有匹配的内容"],
                   sources=[_source("articles", "全文内容库")])


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------
UP_THRESHOLD = 0.5      # |return| under this is "flat"
DOWN_THRESHOLD = -0.5


def score_predictions(db, limit: int = 500) -> dict:
    """Score pending predictions against the next available daily bar."""
    pending = db.prediction_unscored(limit)
    scored, waiting = 0, 0
    for pred in pending:
        secid = pred["secid"]
        base = db.kline_last(secid, pred["as_of_date"])
        nxt = db.kline_next(secid, pred["as_of_date"])
        if not base or not nxt or not base.get("close"):
            waiting += 1
            continue
        realized = (nxt["close"] - base["close"]) / base["close"] * 100
        actual = ("up" if realized > UP_THRESHOLD
                  else "down" if realized < DOWN_THRESHOLD else "flat")
        hit = int(actual == pred["direction"])
        confidence = pred.get("confidence")
        brier = ((confidence - hit) ** 2) if confidence is not None else None
        db.prediction_score(pred["id"], {
            "base_date": base["d"], "base_close": base["close"],
            "result_date": nxt["d"], "result_close": nxt["close"],
            "realized_pct": round(realized, 3), "actual": actual,
            "hit": hit, "brier": round(brier, 4) if brier is not None else None,
        })
        scored += 1
    return {"scored": scored, "waiting_for_bar": waiting, "checked": len(pending)}


# --------------------------------------------------------------------------
# markdown brief (for a human or a chat model without tool calling)
# --------------------------------------------------------------------------
def brief_markdown(regime: dict, candidates: dict, stock: dict | None = None) -> str:
    lines = [f"# T+1 brief — {regime['trade_date']}", ""]
    for fact in regime["facts"]:
        kind = fact["kind"]
        if kind == "indices":
            movers = sorted(fact["items"], key=lambda i: -(i.get("pct") or 0))
            top = ", ".join(f"{i['name']} {i['pct']:+.2f}%" for i in movers[:5])
            lines.append(f"- **海外/指数**: {top}")
        elif kind == "breadth":
            lines.append(f"- **A股广度**: 涨 {fact['up']} / 跌 {fact['down']} / "
                         f"平 {fact['flat']}，均涨 {fact['avg_pct']}%")
        elif kind == "limit_up_pool":
            lines.append(f"- **涨停**: {fact['count']} 家，最高 {fact['max_boards']} 板，"
                         f"连板 {fact['multi_board']} 家")
        elif kind == "sector_flow_top":
            names = ", ".join(f"{i['name']}({(i['main_net'] or 0) / 1e8:.1f}亿)"
                              for i in fact["items"][:5])
            lines.append(f"- **板块流入**: {names}")
        elif kind == "connect":
            parts = []
            for i in fact["items"]:
                if abs(i["net_buy"] or 0) > 1e4:
                    parts.append(f"{i['channel']} 净买 {(i['net_buy'] or 0) / 1e8:+.2f}亿")
                else:
                    parts.append(f"{i['channel']} 成交 {(i['turnover'] or 0) / 1e8:.1f}亿")
            lines.append("- **沪深港通**: " + ", ".join(parts))
    lines += ["", "## 候选", ""]
    for cand in candidates["candidates"][:12]:
        why = "; ".join(f"{r['signal']}={r['value']}" for r in cand["reasons"][:4])
        lines.append(f"- **{cand['name']}** ({cand['secid']}) score {cand['score']} — {why}")
    if stock:
        lines += ["", f"## {stock.get('name')}", ""]
        for fact in stock["facts"]:
            if fact["kind"] == "price":
                lines.append(f"- 收盘 {fact['close']} ({fact['pct']:+.2f}%) on {fact['date']}")
            elif fact["kind"] == "momentum":
                lines.append(f"- 1日 {fact['ret_1d']:+.2f}% / 5日 {fact['ret_5d']:+.2f}% / "
                             f"20日 {fact['ret_20d']}%，距20日高 {fact['from_high_pct']:+.2f}%")
    if regime["caveats"]:
        lines += ["", "## 注意", ""] + [f"- {c}" for c in regime["caveats"]]
    return "\n".join(lines)
