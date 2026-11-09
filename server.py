"""HTTP API + static frontend for the EastMoney monitor."""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
import mimetypes
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from config import SETTINGS, WEB_DIR
from em_client import num
from markets import GROUPS, HEADLINES, MARKET_MAP, MARKETS
import evidence
import intel
import sessions

log = logging.getLogger("em.server")

MAX_PAGE_SIZE = 500
BODY_SECTIONS = {"headline", "viewpoint", "global"}

AS_OF_PARAM = {
    "name": "as_of", "in": "query", "required": False,
    "schema": {"type": "string"},
    "description": "Knowledge cutoff, YYYY-MM-DD or ISO datetime (Asia/Shanghai). "
                   "Defaults to the last session close. Nothing published after "
                   "this instant appears in the response.",
}


def _op(summary: str, description: str, params: list[dict] | None = None) -> dict:
    return {
        "summary": summary,
        "description": description,
        "parameters": params or [],
        "responses": {"200": {"description": "evidence packet",
                              "content": {"application/json": {
                                  "schema": {"$ref": "#/components/schemas/Packet"}}}}},
    }


OPENAPI: dict = {
    "openapi": "3.1.0",
    "info": {
        "title": "EastMoney T+1 evidence protocol",
        "version": "1.0.0",
        "description": (
            "Point-in-time market evidence for an LLM doing next-session prediction. "
            "Every response is an evidence packet: typed facts, explicit units, "
            "source ids, and a caveat list for anything that was not yet published."
        ),
    },
    "servers": [{"url": "http://127.0.0.1:8787"}],
    "paths": {
        "/api/v1/meta": {"get": _op("Protocol metadata",
                                    "Schema version, units, field glossary and endpoint list.")},
        "/api/v1/regime": {"get": _op(
            "Market regime",
            "Breadth, limit-up pool, sector flows, connect flows, margin totals and news "
            "volume for the session ending at as_of.", [AS_OF_PARAM])},
        "/api/v1/candidates": {"get": _op(
            "Ranked candidates",
            "Stocks ranked by signal confluence with per-name reasons and weights. This is "
            "attention ranking, not a return forecast.",
            [AS_OF_PARAM, {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}])},
        "/api/v1/stock/{secid}/evidence": {"get": _op(
            "Single-stock evidence",
            "Price, momentum, every T+1 signal and related news for one instrument, "
            "all cut at as_of.",
            [{"name": "secid", "in": "path", "required": True, "schema": {"type": "string"}},
             AS_OF_PARAM])},
        "/api/v1/news/search": {"get": _op(
            "Full-text news search",
            "Search the 7-day full-text store, cut at as_of.",
            [{"name": "q", "in": "query", "schema": {"type": "string"}},
             AS_OF_PARAM,
             {"name": "days", "in": "query", "schema": {"type": "integer", "default": 7}},
             {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}])},
        "/api/v1/article/{art_id}": {"get": _op(
            "Article body",
            "Full extracted text. Fetched on demand if the hub has not stored it yet.",
            [{"name": "art_id", "in": "path", "required": True, "schema": {"type": "string"}}])},
        "/api/v1/predictions": {
            "get": _op("List predictions", "Recorded predictions with their realised outcome.",
                       [{"name": "days", "in": "query", "schema": {"type": "integer", "default": 30}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100}},
                        {"name": "model", "in": "query", "schema": {"type": "string"}}]),
            "post": {
                "summary": "Record a prediction",
                "description": "Write-back endpoint. The platform scores the call against the "
                               "next available daily bar and feeds the result into the scorecard.",
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Prediction"}}}},
                "responses": {"200": {"description": "stored"}},
            },
        },
        "/api/v1/predictions/scorecard": {"get": _op(
            "Scorecard",
            "Hit rate, Brier score, per-direction breakdown and confidence calibration.",
            [{"name": "days", "in": "query", "schema": {"type": "integer", "default": 30}}])},
        "/api/v1/predictions/score": {"post": {
            "summary": "Score pending predictions",
            "description": "Scores every unscored prediction whose next daily bar now exists.",
            "responses": {"200": {"description": "score summary"}}}},
        "/api/v1/brief": {"get": {
            "summary": "Markdown brief",
            "description": "One compact document for a chat model without tool calling.",
            "parameters": [AS_OF_PARAM, {"name": "secid", "in": "query", "schema": {"type": "string"}}],
            "responses": {"200": {"description": "markdown",
                                  "content": {"text/markdown": {"schema": {"type": "string"}}}}}}},
        "/api/v1/openapi.json": {"get": _op("This document", "OpenAPI 3.1 description.")},
    },
    "components": {
        "schemas": {
            "Packet": {
                "type": "object",
                "required": ["schema", "kind", "as_of", "trade_date", "generated_at"],
                "properties": {
                    "schema": {"type": "string", "const": "em.evidence/1"},
                    "kind": {"type": "string"},
                    "as_of": {"type": "string"},
                    "trade_date": {"type": "string"},
                    "generated_at": {"type": "string"},
                    "units": {"type": "object"},
                    "facts": {"type": "array", "items": {"type": "object"}},
                    "caveats": {"type": "array", "items": {"type": "string"}},
                    "sources": {"type": "array", "items": {"type": "object"}},
                },
            },
            "Prediction": {
                "type": "object",
                "required": ["secid", "direction"],
                "properties": {
                    "secid": {"type": "string", "example": "0.002131"},
                    "direction": {"type": "string", "enum": ["up", "down", "flat"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "expected_return_pct": {"type": "number"},
                    "horizon_days": {"type": "integer", "default": 1},
                    "as_of": {"type": "string"},
                    "rationale": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                },
            },
        }
    },
}

# shared pool for interactive per-stock fetches
STOCK_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="stock")


def normalise_stock(d: dict, secid: str) -> dict:
    """Flatten the push2 stock snapshot into the shape the UI expects."""
    if not d:
        return {"secid": secid}
    return {
        "secid": secid,
        "code": d.get("f57"),
        "name": d.get("f58"),
        "market": d.get("f107"),
        "industry": d.get("f127"),
        "decimals": d.get("f59"),
        "price": num(d.get("f43")),
        "prev_close": num(d.get("f60")),
        "open": num(d.get("f46")),
        "high": num(d.get("f44")),
        "low": num(d.get("f45")),
        "change": num(d.get("f169")),
        "pct": num(d.get("f170")),
        "amplitude": num(d.get("f171")),
        "volume": num(d.get("f47")),
        "amount": num(d.get("f48")),
        "turnover": num(d.get("f168")),
        "volume_ratio": num(d.get("f50")),
        "avg_price": num(d.get("f71")),
        "limit_up": num(d.get("f51")),
        "limit_down": num(d.get("f52")),
        "bid_ratio": num(d.get("f191")),
        "bid_diff": num(d.get("f192")),
        "outer": num(d.get("f49")),
        "inner": num(d.get("f161")),
        "mcap": num(d.get("f116")),
        "float_mcap": num(d.get("f117")),
        "shares": num(d.get("f84")),
        "float_shares": num(d.get("f85")),
        "pe": num(d.get("f162")),
        "pe_ttm": num(d.get("f164")),
        "pe_static": num(d.get("f163")),
        "pb": num(d.get("f167")),
        "bvps": num(d.get("f92")),
        "status": num(d.get("f292")),
        "ts": num(d.get("f86")),
    }


def order_book(d: dict) -> dict:
    """Five-level ladder. asks run from 卖1 to 卖5, bids from 买1 to 买5."""
    if not d:
        return {"asks": [], "bids": []}
    ask_pairs = (("f39", "f40"), ("f37", "f38"), ("f35", "f36"), ("f33", "f34"), ("f31", "f32"))
    bid_pairs = (("f19", "f20"), ("f17", "f18"), ("f15", "f16"), ("f13", "f14"), ("f11", "f12"))
    asks = [{"price": num(d.get(p)), "volume": num(d.get(v))} for p, v in ask_pairs]
    bids = [{"price": num(d.get(p)), "volume": num(d.get(v))} for p, v in bid_pairs]
    return {"asks": [a for a in asks if a["price"]],
            "bids": [b for b in bids if b["price"]]}


class Monitor:
    """Read model over the database plus a handle to trigger collections."""

    def __init__(self, settings, db, collector=None, pool=None, client=None, clock=None,
                 hub=None, intel_hub=None):
        self.cfg = settings
        self.db = db
        self.collector = collector
        self.pool = pool
        self.client = client
        self.clock = clock
        self.hub = hub
        self.intel_hub = intel_hub
        self._refresh_lock = threading.Lock()
        self._refreshing: set[str] = set()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._prefetching: set[str] = set()

    def signal_summary(self) -> dict:
        return self._cached("signals", 5.0, self._signal_summary_uncached)

    def _signal_summary_uncached(self) -> dict:
        payload = self.db.signal_summary()
        payload["hub"] = self.hub.status() if self.hub else None
        payload["retention_days"] = self.cfg.signal_retention_days
        return payload

    def article(self, art_id: str) -> dict | None:
        """Return an article, fetching its body on demand if we only have the headline."""
        item = self.db.article(art_id)
        if not item:
            return None
        if not item.get("body") and item.get("url") and self.client is not None \
                and item["section"] in BODY_SECTIONS:
            try:
                body = intel.fetch_body(self.client, item["url"])
                if body:
                    self.db.upsert_articles([{
                        "art_id": item["art_id"], "section": item["section"],
                        "title": item["title"], "url": item["url"],
                        "published": item.get("published") or 0, "body": body}])
                    item = self.db.article(art_id) or item
            except Exception as exc:
                log.debug("on-demand body %s failed: %s", art_id, exc)
        return item

    # -- v1: agent-facing evidence protocol -------------------------------
    def _as_of(self, text: str | None) -> evidence.AsOf:
        return evidence.resolve_as_of(text, self.now())

    def v1_meta(self) -> dict:
        latest = self.db.article_summary()
        return {
            "schema": evidence.SCHEMA,
            "name": "EastMoney T+1 evidence protocol",
            "as_of_default": self._as_of(None).iso,
            "units": evidence.UNITS,
            "glossary": evidence.GLOSSARY,
            "rules": [
                "Every query accepts as_of (YYYY-MM-DD or ISO datetime, Asia/Shanghai).",
                "Only facts published at or before as_of are returned.",
                "Absent data is listed in `caveats`, never silently replaced.",
                "Default as_of is the last session close, not now.",
            ],
            "coverage": {"articles_7d": latest["total"],
                         "signals": self.db.signal_summary()["window"]},
            "endpoints": [
                "GET /api/v1/regime", "GET /api/v1/candidates",
                "GET /api/v1/stock/{secid}/evidence", "GET /api/v1/news/search",
                "GET /api/v1/article/{art_id}", "POST /api/v1/predictions",
                "GET /api/v1/predictions", "GET /api/v1/predictions/scorecard",
                "POST /api/v1/predictions/score", "GET /api/v1/brief",
                "GET /api/v1/openapi.json",
            ],
        }

    def v1_regime(self, as_of_text: str | None) -> dict:
        return evidence.build_regime(self.db, self._as_of(as_of_text), HEADLINES)

    def v1_candidates(self, as_of_text: str | None, limit: int = 20) -> dict:
        return evidence.build_candidates(self.db, self._as_of(as_of_text), limit)

    def ensure_klines(self, secid: str, need: int = 25) -> int:
        """Daily bars are only collected for a watch set; fill one on demand.

        Evidence quality and, more importantly, scoring both need a bar on the
        predicted date - without this, predictions on unlisted names could never
        be scored.
        """
        today = time.strftime("%Y-%m-%d")
        bars = self.db.kline_recent(secid, today, need)
        if self.client is None:
            return len(bars)
        if len(bars) >= need:
            # evidence and scoring key off the newest bar, so keep it honest
            if self._bars_stale(bars) and self._refresh_bars(secid, 101, max(need, 180)):
                return len(self.db.kline_recent(secid, today, need))
            return len(bars)
        try:
            rows = self.client.kline(secid, klt=101, limit=need + 15)
        except Exception as exc:                       # noqa: BLE001 - best effort
            log.debug("kline fetch for %s failed: %s", secid, exc)
            return len(bars)
        if rows:
            self.db.save_kline(secid, 101, rows)
        return max(len(bars), len(rows or []))

    def v1_stock(self, secid: str, as_of_text: str | None) -> dict:
        secid = self.resolve(secid)
        if not re.fullmatch(r"\d+\.[A-Za-z0-9]+", secid):
            raise ValueError(f"invalid secid {secid!r}; expected market.code like 1.600519")
        self.ensure_klines(secid)
        return evidence.build_stock(self.db, secid, self._as_of(as_of_text))

    def v1_news(self, q: str | None, as_of_text: str | None, days: int, limit: int) -> dict:
        return evidence.build_news(self.db, self._as_of(as_of_text), q, days, limit)

    def v1_brief(self, as_of_text: str | None, secid: str | None = None) -> str:
        as_of = self._as_of(as_of_text)
        regime = evidence.build_regime(self.db, as_of, HEADLINES)
        candidates = evidence.build_candidates(self.db, as_of, 12)
        stock = evidence.build_stock(self.db, self.resolve(secid), as_of) if secid else None
        return evidence.brief_markdown(regime, candidates, stock)

    def v1_predict(self, payload: dict) -> dict:
        if not payload.get("secid") or not payload.get("direction"):
            raise ValueError("secid and direction are required")
        direction = str(payload["direction"]).lower()
        if direction not in ("up", "down", "flat"):
            raise ValueError("direction must be up, down or flat")
        confidence = payload.get("confidence")
        if confidence is not None:
            confidence = float(confidence)
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("confidence must be within 0..1")
        as_of = self._as_of(payload.get("as_of"))
        secid = self.resolve(payload["secid"])
        self.ensure_klines(secid)
        quote = self.db.quote(secid)
        pred_id = self.db.prediction_add({
            "as_of": as_of.iso, "as_of_date": as_of.date, "secid": secid,
            "name": payload.get("name") or (quote or {}).get("name"),
            "direction": direction, "confidence": confidence,
            "expected_return_pct": payload.get("expected_return_pct"),
            "horizon_days": int(payload.get("horizon_days") or 1),
            "rationale": (payload.get("rationale") or "")[:4000],
            "evidence": payload.get("evidence") or [],
            "model": (payload.get("model") or "")[:80],
        })
        return {"ok": True, "id": pred_id, "as_of": as_of.iso,
                "note": "scored automatically once the next daily bar exists"}

    def v1_predictions(self, days: int, limit: int, model: str | None) -> dict:
        rows = self.db.predictions(days=days, limit=limit, model=model)
        return {"count": len(rows), "predictions": rows}

    def v1_scorecard(self, days: int = 30) -> dict:
        return self.db.prediction_scorecard(days)

    def v1_score(self) -> dict:
        return evidence.score_predictions(self.db)

    def openapi(self) -> dict:
        return OPENAPI

    def intel_sections(self) -> dict:
        summary = self.db.article_summary()
        hub = self.intel_hub.status() if self.intel_hub else None
        return {"sections": [
            {**sec, "count": (summary["sections"].get(sec["id"]) or {}).get("count", 0),
             "full_text": (summary["sections"].get(sec["id"]) or {}).get("full_text", 0)}
            for sec in (hub["sections"] if hub else [])
        ], "total": summary["total"], "hub": hub, "retention_days": self.cfg.signal_retention_days}

    def intel_digest(self, days: int = 1, limit: int = 30) -> dict:
        """Compact feed for other agents: headline + first 240 chars + link."""
        rows = self.db.articles(days=days, limit=limit, with_body=True)
        items = []
        for r in rows:
            text = r.get("body") or r.get("summary") or ""
            items.append({"art_id": r["art_id"], "section": r["section"],
                          "published": r["published"], "title": r["title"],
                          "media": r.get("media"), "chars": r.get("words") or 0,
                          "excerpt": text[:240], "url": r.get("url"),
                          "stocks": r.get("stocks") or []})
        return {"days": days, "count": len(items), "items": items}

    def now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def _cached(self, key: str, ttl: float, producer):
        """TTL cache with single-flight: concurrent misses share one producer."""
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            waiter = self._inflight.get(key)
            leader = waiter is None
            if leader:
                waiter = threading.Event()
                self._inflight[key] = waiter
        if not leader:
            # somebody is already fetching this (e.g. a hover prefetch): wait
            waiter.wait(timeout=15)
            with self._cache_lock:
                hit = self._cache.get(key)
            if hit:
                return hit[1]
            return producer()
        try:
            value = producer()
            with self._cache_lock:
                self._cache[key] = (time.time(), value)
            return value
        finally:
            with self._cache_lock:
                self._inflight.pop(key, None)
            waiter.set()

    def start_refresher(self, interval: float = 2.0) -> threading.Event:
        """Keep the dashboard payloads hot so requests are pure cache reads."""
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                for key, producer in (
                    ("overview", self._overview_uncached),
                    ("status", self._status_uncached),
                    ("watchlist", self._watchlist_uncached),
                ):
                    try:
                        value = producer()
                        with self._cache_lock:
                            self._cache[key] = (time.time(), value)
                    except Exception as exc:
                        log.debug("refresh %s failed: %s", key, exc)
                stop.wait(interval)

        threading.Thread(target=loop, name="cache-warmer", daemon=True).start()
        return stop

    # -- helpers ---------------------------------------------------------
    def _market_meta(self) -> dict[str, dict]:
        out = {}
        for row in self.db.markets():
            m = MARKET_MAP.get(row["id"])
            stats = {}
            out[row["id"]] = {
                "id": row["id"],
                "group": row["grp"],
                "name": row["name"],
                "kind": row["kind"],
                "interval": row["interval"],
                "history": bool(row["history"]),
                "fs": row["fs"],
                "expected": m.total if m else 0,
                "primary": bool(m.primary) if m else False,
                "stats": stats,
            }
        return out

    def overview(self) -> dict:
        return self._cached("overview", 8.0, self._overview_uncached)

    def _overview_uncached(self) -> dict:
        now = int(self.now())
        markets = self._market_meta()
        stats = self.db.market_stats_latest()
        states = self.db.collect_states()
        groups = []
        for gid, gname, gdesc in GROUPS:
            items = [m for m in markets.values() if m["group"] == gid]
            items.sort(key=lambda m: next(i for i, x in enumerate(MARKETS) if x.id == m["id"]))
            rows = []
            for m in items:
                st = stats.get(m["id"], {})
                state = states.get(m["id"], {})
                age = now - (state.get("last_ts") or 0) if state else None
                meta = MARKET_MAP.get(m["id"])
                session = sessions.describe(meta.session, now) if meta else None
                rows.append({
                    **m,
                    "stats": st,
                    "session": session,
                    "updated_at": state.get("last_ts"),
                    "age": age,
                    "stale": age is None or (session and session["open"] and age > m["interval"] * 4),
                    "ok": bool(state.get("last_ok")),
                    "error": state.get("last_error"),
                    "stored": state.get("rows"),
                })
            groups.append({"id": gid, "name": gname, "desc": gdesc, "markets": rows})

        tape_rows = {r["secid"]: r for r in self.db.by_secids([s for s, _ in HEADLINES])}
        tape = []
        for secid, label in HEADLINES:
            row = tape_rows.get(secid)
            tape.append({
                "secid": secid,
                "name": (row or {}).get("name") or label,
                "price": (row or {}).get("price"),
                "pct": (row or {}).get("pct"),
                "chg": (row or {}).get("chg"),
                "updated_at": (row or {}).get("updated_at"),
            })

        movers = self.db.movers(limit=12, kind=None)
        return {
            "generated_at": now,
            "server_utc": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "groups": groups,
            "tape": tape,
            "movers": movers,
            "counts": self.db.counts(),
            "runs": self.db.recent_runs(5),
            "proxy": self.pool.status() if self.pool else {"enabled": False},
            "clock": self.clock.status() if self.clock else None,
            "collector": {
                "refreshing": sorted(self._refreshing),
                "markets": len(MARKETS),
                "open_markets": sum(1 for g in groups for m in g["markets"]
                                    if m["session"] and m["session"]["open"]),
                "history": self.cfg.history_enabled,
            },
        }

    def market_rows(self, market_id: str, query: dict) -> dict:
        meta = MARKET_MAP.get(market_id)
        if not meta:
            raise KeyError(market_id)
        page = max(1, _int(query.get("page"), 1))
        size = min(MAX_PAGE_SIZE, max(5, _int(query.get("size"), 50)))
        sort = query.get("sort") or "pct"
        order = query.get("order") or "desc"
        search = (query.get("q") or "").strip()
        rows, total = self.db.rows_for_market(market_id, page=page, size=size,
                                              sort=sort, order=order, search=search)
        stats = self.db.market_stats_latest().get(market_id, {})
        state = self.db.collect_states().get(market_id, {})
        return {
            "market": {"id": meta.id, "name": meta.name, "group": meta.group, "kind": meta.kind,
                       "interval": meta.interval, "expected": meta.total},
            "session": sessions.describe(meta.session, self.now()),
            "rows": rows,
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size if total else 1,
            "sort": sort,
            "order": order,
            "stats": stats,
            "updated_at": state.get("last_ts"),
            "error": state.get("last_error"),
        }

    def instrument(self, secid: str) -> dict:
        row = self.db.instrument(secid)
        if not row:
            raise KeyError(secid)
        market_id = row.get("market_id")
        meta = MARKET_MAP.get(market_id)
        return {
            "instrument": row,
            "market": {"id": meta.id, "name": meta.name} if meta else None,
            "intraday": self.db.intraday(secid, minutes=1440),
            "kline": self.db.kline(secid, 101, 180),
            "peers": self.db.rows_for_market(market_id, page=1, size=8, sort="pct",
                                             order="desc")[0] if market_id else [],
        }

    def status(self) -> dict:
        return self._cached("status", 8.0, self._status_uncached)

    def _status_uncached(self) -> dict:
        stats = self.db.market_stats_latest()
        states = self.db.collect_states()
        now = int(self.now())
        fresh = sum(1 for s in states.values() if now - (s.get("last_ts") or 0) < 600)
        runs = [r for r in self.db.recent_runs(12) if r.get("finished")]
        open_markets = 0
        session_rows = []
        for m in MARKETS:
            st = sessions.describe(m.session, now)
            if st["open"]:
                open_markets += 1
            session_rows.append({"market": m.id, "name": m.name, **st})
        return {
            "time": now,
            "server_utc": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": self.db.counts(),
            "markets_live": fresh,
            "markets_total": len(states),
            "markets_open": open_markets,
            "last_run": runs[0] if runs else None,
            "clock": self.clock.status() if self.clock else None,
            "sessions": session_rows,
            "proxy": self.pool.status() if self.pool else {"enabled": False},
            "proxy_db": self.db.proxy_summary(),
            "client": self.client.stats if self.client else {},
            "signals": self.db.signal_summary(),
            "intel": self.db.article_summary(),
            "runs": runs,
            "events": self.db.recent_events(25),
        }

    def stock_detail(self, secid: str) -> dict:
        """Everything the stock terminal needs, assembled with per-part TTLs.

        Quote/depth and ticks refresh every few seconds, capital flow every
        minute, bars every ten minutes — so a click costs at most one or two
        proxied round trips and repeats are free.
        """
        if self.client is None:
            raise RuntimeError("live client not attached")
        secid = self.resolve(secid)
        return self._cached(f"stock:{secid}", 2.0, lambda: self._stock_detail(secid))

    def prefetch(self, secid: str) -> dict:
        """Warm the cache on hover so the click feels instant."""
        secid = self.resolve(secid)
        with self._cache_lock:
            hit = self._cache.get(f"stock:{secid}")
            if hit and time.time() - hit[0] < 4:
                return {"started": False, "cached": True, "secid": secid}
            if secid in self._prefetching:
                return {"started": False, "running": True, "secid": secid}
            self._prefetching.add(secid)

        def work():
            try:
                self.stock_detail(secid)
            except Exception as exc:  # prefetch must never surface errors
                log.debug("prefetch %s failed: %s", secid, exc)
            finally:
                with self._cache_lock:
                    self._prefetching.discard(secid)

        threading.Thread(target=work, name=f"prefetch-{secid}", daemon=True).start()
        return {"started": True, "secid": secid}

    def _stock_detail(self, secid: str) -> dict:
        """Assemble a stock payload with a hard budget.

        Live parts are fetched in parallel; whatever has not arrived inside the
        budget is returned as the previous value (or omitted) and the response
        still goes out. Late results land in the cache, so the drawer's 5 s poll
        completes the picture without ever making the user wait on one slow IP.
        """
        base = self.db.instrument(secid) or {}
        snapshot = self.db.cache_get(secid, "quote", 4)
        flow = self.db.cache_get(secid, "flow", 60)
        ticks = self.db.cache_get(secid, "trades", 5)
        trends = self.db.cache_get(secid, "trends", 30)

        jobs: dict[str, Any] = {}
        if snapshot is None:
            jobs["quote"] = lambda: self.client.stock(secid)
        if flow is None:
            jobs["flow"] = lambda: self.client.flow(secid, 1)
        if ticks is None:
            jobs["trades"] = lambda: self.client.trades(secid, -40)
        if trends is None:
            jobs["trends"] = lambda: self.client.trends(secid, 1)

        errors: dict[str, str] = {}
        pending = 0
        if jobs:
            futures = {STOCK_EXECUTOR.submit(fn): name for name, fn in jobs.items()}
            # first half of the budget: let the normal attempts run
            half = max(0.25, self.cfg.stock_budget / 2)
            done, still = wait(futures, timeout=half)
            # second half: race a duplicate of every straggler through another IP
            hedges = {STOCK_EXECUTOR.submit(jobs[futures[f]]): futures[f] for f in still}
            done2, still2 = wait(list(still) + list(hedges),
                                 timeout=max(0.0, self.cfg.stock_budget - half))
            results = {}
            for fut in list(done) + list(done2):
                name = futures.get(fut) or hedges.get(fut)
                try:
                    results[name] = fut.result()
                except Exception as exc:
                    errors.setdefault(name, str(exc))
            for name, value in results.items():
                self.db.cache_put(secid, name, value)
                if name == "quote":
                    snapshot = value
                elif name == "flow":
                    flow = value
                elif name == "trades":
                    ticks = value
                elif name == "trends":
                    trends = value
            for fut in still2:
                job_name = futures.get(fut) or hedges.get(fut)
                fut.add_done_callback(
                    lambda done_fut, n=job_name: self._late_cache(secid, n, done_fut))
            pending = len([f for f in still2 if (futures.get(f) or hedges.get(f)) not in results])

        snapshot = snapshot or self.db.cache_get(secid, "quote", 3600) or {}
        flow = flow or self.db.cache_get(secid, "flow", 3600) or []
        ticks = ticks or []
        trends = trends or {}
        payload = normalise_stock(snapshot, secid)
        # fall back to the last collected snapshot so the first paint is instant
        for field in ("price", "prev_close", "open", "high", "low", "change", "pct",
                      "volume", "amount", "turnover", "mcap", "pe", "pb", "float_mcap"):
            if payload.get(field) is None and base.get(field) is not None:
                payload[field] = base[field]
        payload["name"] = payload.get("name") or base.get("name")
        payload["code"] = payload.get("code") or base.get("code")
        payload["depth"] = order_book(snapshot)
        payload["flow"] = flow[-240:]
        payload["trades"] = ticks
        payload["trends"] = trends.get("points") or []
        payload["pre_close"] = payload.get("prev_close") or trends.get("preClose")
        payload["watched"] = any(w["secid"] == secid for w in self.db.watchlist())
        # HK flow is reported on a different scale, so guard against absurd values
        last_flow = abs((payload["flow"] or [{}])[-1].get("main") or 0)
        amount = abs(payload.get("amount") or 0)
        payload["flow_reliable"] = bool(payload["flow"]) and (
            amount == 0 or last_flow <= amount * 3)
        payload["errors"] = errors
        payload["partial"] = pending > 0
        return payload

    def _late_cache(self, secid: str, name: str, fut) -> None:
        try:
            self.db.cache_put(secid, name, fut.result())
        except Exception:
            pass

    def resolve(self, secid: str) -> str:
        """Accept 600519, sh600519 or 1.600519 and return a push2 secid."""
        token = secid.strip()
        if "." in token:
            return token
        lowered = token.lower()
        if lowered.startswith(("sh", "sz", "bj")):
            code, prefix = lowered[2:], lowered[:2]
            market = {"sh": "1", "sz": "0", "bj": "0"}[prefix]
            return f"{market}.{code}"
        row = self.db.reader().execute(
            "SELECT secid FROM quote_latest WHERE code = ? LIMIT 1", (token,)).fetchone()
        return row["secid"] if row else token

    def stock_flow_daily(self, secid: str, limit: int = 20) -> dict:
        secid = self.resolve(secid)
        cached = self.db.cache_get(secid, "flow_daily", 300)
        if cached is None and self.client is not None:
            try:
                cached = self.client.flow(secid, 101, hedge_ms=300)
                self.db.cache_put(secid, "flow_daily", cached)
            except Exception as exc:
                log.warning("daily flow %s failed: %s", secid, exc)
                cached = []
        return {"secid": secid, "rows": (cached or [])[-limit:]}

    def watchlist(self) -> dict:
        return self._cached("watchlist", 8.0, self._watchlist_uncached)

    def _watchlist_uncached(self) -> dict:
        items = []
        rows = {r["secid"]: r for r in self.db.by_secids([w["secid"] for w in self.db.watchlist()])}
        for w in self.db.watchlist():
            quote = rows.get(w["secid"], {})
            items.append({
                "secid": w["secid"], "name": quote.get("name") or w["name"],
                "code": quote.get("code") or w["secid"].split(".")[-1],
                "price": quote.get("price"), "pct": quote.get("pct"),
                "chg": quote.get("chg"), "amount": quote.get("amount"),
                "kind": quote.get("kind"), "updated_at": quote.get("updated_at"),
            })
        return {"items": items}

    # -- daily bars: freshness ------------------------------------------
    def _cn_now(self) -> datetime:
        now = self.now()
        return datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))

    def _bars_stale(self, bars: list[dict]) -> bool:
        """True when the newest bar is from the current or the previous session.

        Such a bar may still move (intraday) or have been captured before the
        close, so serving it forever would hand an agent a wrong last candle.
        """
        if not bars:
            return True
        today = self._cn_now().date()
        try:
            last = datetime.strptime(bars[-1]["d"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            return True
        return (today - last).days <= 3

    def _refresh_bars(self, secid: str, klt: int, limit: int) -> bool:
        """Refetch at most once a minute per (secid, klt) while the bar is live."""
        if self.client is None:
            return False
        cache = getattr(self, "_live_bars", None)
        if cache is None:
            cache = self._live_bars = {}
        key = (secid, klt)
        stamp, ok = cache.get(key, (0.0, False))
        if time.time() - stamp < 60:
            return ok
        try:
            bars = self.client.kline(secid, klt=klt, limit=max(limit, 180), hedge_ms=300)
        except Exception as exc:                   # noqa: BLE001 - provider hiccup
            log.warning("kline refresh %s klt=%s failed: %s", secid, klt, exc)
            cache[key] = (time.time(), False)
            return False
        if bars:
            self.db.save_kline(secid, klt, bars)
        cache[key] = (time.time(), bool(bars))
        return bool(bars)

    def instruments(self, query: dict) -> dict:
        """Flat, paginated enumeration of every symbol the platform knows."""
        page = max(1, _int(query.get("page"), 1))
        size = min(1000, max(1, _int(query.get("size"), 500)))
        market = query.get("market") or None
        if market and market not in MARKET_MAP:
            raise KeyError(market)
        kind = query.get("kind") or None
        rows, total = self.db.instruments_page(
            page=page, size=size, market=market, kind=kind,
            q=(query.get("q") or "").strip() or None, sort=query.get("sort") or "amount")
        pages = (total + size - 1) // size if total else 1
        tail = (f"&market={market}" if market else "") + (f"&kind={kind}" if kind else "")
        return {"page": page, "size": size, "pages": pages, "total": total,
                "market": market, "kind": kind, "rows": rows,
                "next": (f"/api/instruments?page={page + 1}&size={size}{tail}"
                         if page < pages else None)}

    def stock_bars(self, secid: str, klt: int = 101, limit: int = 180,
                   start: str | None = None, end: str | None = None) -> dict:
        secid = self.resolve(secid)
        ranged = bool(start or end)
        low, high = (start or "1990-01-01"), (end or "2999-12-31")
        cached = (self.db.kline_range(secid, low, high, klt, min(2000, limit))
                  if ranged else self.db.kline(secid, klt, limit))
        refreshed = False
        if cached and self._bars_stale(cached) and self._refresh_bars(secid, klt, max(limit, 180)):
            cached = (self.db.kline_range(secid, low, high, klt, min(2000, limit))
                      if ranged else self.db.kline(secid, klt, limit))
            refreshed = True
        now = self._cn_now()
        last = cached[-1]["d"] if cached else None
        final = True
        if last:
            try:
                last_date = datetime.strptime(last, "%Y-%m-%d").date()
                final = last_date < now.date() or (now.hour, now.minute) >= (15, 0)
            except ValueError:
                final = True
        return {"secid": secid, "klt": klt, "bars": cached, "live": refreshed,
                "as_of": now.isoformat(), "last_date": last, "last_final": final,
                "from": start, "to": end,
                "note": None if final else "最后一根为当日未收盘K线，收盘前会继续变化"}

    def trigger(self, market_id: str | None) -> dict:
        if market_id and market_id not in MARKET_MAP:
            raise KeyError(market_id)
        if self.collector is None:
            raise RuntimeError("collector not attached")
        if not self._refresh_lock.acquire(blocking=False):
            return {"accepted": False, "reason": "a refresh is already running"}
        try:
            target = [MARKET_MAP[market_id]] if market_id else None
            if target:
                self._refreshing.add(market_id)
                try:
                    result = self.collector.collect_market(target[0])
                finally:
                    self._refreshing.discard(market_id)
            else:
                result = self.collector.run_once(only=None, force=True)
            return {"accepted": True, "result": result}
        finally:
            self._refresh_lock.release()

    def export_csv(self, market_id: str) -> tuple[str, str]:
        meta = MARKET_MAP.get(market_id)
        if not meta:
            raise KeyError(market_id)
        rows, _ = self.db.rows_for_market(market_id, page=1, size=MAX_PAGE_SIZE,
                                          sort="pct", order="desc")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["code", "name", "price", "pct", "chg", "open", "high", "low",
                         "prev_close", "volume", "amount", "turnover", "pe", "pb", "mcap"])
        for r in rows:
            writer.writerow([r.get("code"), r.get("name"), r.get("price"), r.get("pct"),
                             r.get("chg"), r.get("open"), r.get("high"), r.get("low"),
                             r.get("prev_close"), r.get("volume"), r.get("amount"),
                             r.get("turnover"), r.get("pe"), r.get("pb"), r.get("mcap")])
        return f"{meta.id.replace('.', '_')}.csv", buf.getvalue()


def _int(value, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "EastMoneyMonitor/1.1"
    protocol_version = "HTTP/1.1"
    app: Monitor  # injected

    def log_message(self, fmt, *args):  # quieter access log
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- plumbing --------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        headers = {"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"}
        headers.update(extra or {})
        accept = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept and len(body) > 1024 and ctype.startswith(("application/json", "text/")):
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # the page navigated away or the drawer closed mid-response
            self.close_connection = True

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message, "status": code}, code)

    def _static(self, name: str) -> None:
        rel = name.lstrip("/") or "index.html"
        if rel in ("", "/"):
            rel = "index.html"
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        self._send(200, body, ctype)

    # -- routing ---------------------------------------------------------
    def _v1(self, path: str, query: dict, body: bytes | None) -> None:
        """Agent-facing evidence routes (see OPENAPI)."""
        app = self.app
        try:
            if path == "/api/v1/meta":
                self._json(app.v1_meta())
            elif path == "/api/v1/openapi.json":
                self._send(200, json.dumps(OPENAPI, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path == "/api/v1/brief":
                text = app.v1_brief(query.get("as_of"), query.get("secid"))
                self._send(200, text.encode("utf-8"), "text/markdown; charset=utf-8")
            elif path == "/api/v1/regime":
                self._json(app.v1_regime(query.get("as_of")))
            elif path == "/api/v1/candidates":
                self._json(app.v1_candidates(query.get("as_of"),
                                             min(100, max(1, _int(query.get("limit"), 20)))))
            elif path.startswith("/api/v1/stock/") and path.endswith("/evidence"):
                secid = urllib.parse.unquote(path[len("/api/v1/stock/"):-len("/evidence")])
                self._json(app.v1_stock(secid, query.get("as_of")))
            elif path == "/api/v1/news/search":
                self._json(app.v1_news(query.get("q"), query.get("as_of"),
                                       min(7, max(1, _int(query.get("days"), 7))),
                                       min(100, max(1, _int(query.get("limit"), 20)))))
            elif path.startswith("/api/v1/article/"):
                art_id = urllib.parse.unquote(path[len("/api/v1/article/"):])
                item = app.article(art_id)
                if not item:
                    self._error(404, f"unknown article {art_id}")
                else:
                    self._json(item)
            elif path == "/api/v1/predictions/scorecard":
                self._json(app.v1_scorecard(min(365, max(1, _int(query.get("days"), 30)))))
            elif path == "/api/v1/predictions/score":
                self._json(app.v1_score())
            elif path == "/api/v1/predictions":
                if self.command == "POST":
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    self._json(app.v1_predict(payload), 201)
                else:
                    self._json(app.v1_predictions(
                        min(365, max(1, _int(query.get("days"), 30))),
                        min(500, max(1, _int(query.get("limit"), 100))),
                        query.get("model")))
            else:
                self._error(404, f"unknown v1 route {path}")
        except ValueError as exc:
            self._error(400, str(exc))
        except KeyError as exc:
            self._error(404, f"not found: {exc}")
        except json.JSONDecodeError as exc:
            self._error(400, f"invalid JSON body: {exc.msg}")
        except Exception as exc:                       # noqa: BLE001 - surfaced to the caller
            log.exception("v1 route %s failed", path)
            self._error(500, f"{type(exc).__name__}: {exc}")

    def handle_one_request(self) -> None:
        # the browser drops keep-alive sockets constantly; that is not an error
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/v1/"):
                self._v1(path, query, None)
            elif path == "/" or path == "/index.html":
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path in ("/app.js", "/styles.css", "/favicon.svg", "/favicon.ico"):
                self._static(path.lstrip("/"))
            elif path == "/api/overview":
                self._json(self.app.overview())
            elif path == "/api/markets":
                counts = self.app.db.market_counts()
                groups = [
                    {"id": g[0], "name": g[1], "desc": g[2],
                     "markets": [{"id": m.id, "name": m.name, "kind": m.kind,
                                  "interval": m.interval, "count": counts.get(m.id, 0),
                                  "session": sessions.describe(m.session, self.app.now())
                                  if m.session else None}
                                 for m in MARKETS if m.group == g[0]]}
                    for g in GROUPS]
                self._json({"groups": groups,
                            "markets": len(MARKETS),
                            "memberships": sum(counts.values()),
                            "unique_instruments": self.app.db.instrument_count()})
            elif path.startswith("/api/market/"):
                mid = urllib.parse.unquote(path[len("/api/market/"):])
                try:
                    self._json(self.app.market_rows(mid, query))
                except KeyError:
                    self._error(404, f"unknown market {mid}")
            elif path.startswith("/api/instrument/"):
                secid = urllib.parse.unquote(path[len("/api/instrument/"):])
                try:
                    self._json(self.app.instrument(secid))
                except KeyError:
                    self._error(404, f"unknown instrument {secid}")
            elif path.startswith("/api/kline/"):
                secid = urllib.parse.unquote(path[len("/api/kline/"):])
                self._json(self.app.stock_bars(secid, _int(query.get("klt"), 101),
                                               min(400, max(30, _int(query.get("limit"), 120)))))
            elif path == "/api/movers":
                limit = min(100, max(3, _int(query.get("limit"), 20)))
                kind = query.get("kind")
                mids = [m for m in query.get("markets", "").split(",") if m]
                self._json(self.app.db.movers(limit=limit, market_ids=mids or None, kind=kind))
            elif path == "/api/intel/sections":
                self._json(self.app.intel_sections())
            elif path == "/api/intel/digest":
                self._json(self.app.intel_digest(min(7, max(1, _int(query.get("days"), 1))),
                                                 min(200, max(5, _int(query.get("limit"), 30)))))
            elif path.startswith("/api/intel/article/"):
                art_id = urllib.parse.unquote(path[len("/api/intel/article/"):])
                item = self.app.article(art_id)
                if item:
                    self._json(item)
                else:
                    self._error(404, f"unknown article {art_id}")
            elif path == "/api/intel/articles":
                section = query.get("section") or None
                rows = self.app.db.articles(section=section,
                                            days=min(7, max(1, _int(query.get("days"), 7))),
                                            limit=min(200, max(5, _int(query.get("limit"), 40))),
                                            with_body=query.get("body") in ("1", "true"),
                                            q=(query.get("q") or "").strip() or None)
                self._json({"section": section, "count": len(rows), "items": rows})
            elif path == "/api/signals/summary":
                self._json(self.app.signal_summary())
            elif path.startswith("/api/signals/stock/"):
                secid = self.app.resolve(urllib.parse.unquote(path[len("/api/signals/stock/"):]))
                self._json({"secid": secid, "signals": self.app.db.signals_for(secid, 40)})
            elif path.startswith("/api/signals/"):
                source = urllib.parse.unquote(path[len("/api/signals/"):])
                day = query.get("day") or self.app.db.latest_signal_day(source)
                self._json({"source": source, "day": day,
                            "rows": self.app.db.signals(source, day,
                                                        min(300, max(5, _int(query.get("limit"), 60))),
                                                        query.get("order") or "desc")})
            elif path == "/api/search":
                q = (query.get("q") or "").strip()
                self._json({"query": q, "results": self.app.db.search(q, 25)})
            elif path == "/api/watchlist":
                self._json(self.app.watchlist())
            elif path.startswith("/api/watchlist/"):
                secid = urllib.parse.unquote(path[len("/api/watchlist/"):])
                self._json({"removed": self.app.db.watch_remove(secid)})
            elif path.startswith("/api/stock/"):
                rest = urllib.parse.unquote(path[len("/api/stock/"):])
                if rest.endswith("/kline"):
                    secid = rest[: -len("/kline")]
                    klt = _int(query.get("klt"), 101)
                    if klt not in (101, 102, 103):
                        klt = 101
                    self._json(self.app.stock_bars(
                        secid, klt, min(2000, _int(query.get("limit"), 180)),
                        query.get("from"), query.get("to")))
                elif rest.endswith("/flow"):
                    secid = rest[: -len("/flow")]
                    self._json(self.app.stock_flow_daily(secid, min(120, _int(query.get("limit"), 20))))
                elif rest.endswith("/prefetch"):
                    secid = rest[: -len("/prefetch")]
                    self._json(self.app.prefetch(secid), 202)
                else:
                    try:
                        self._json(self.app.stock_detail(rest))
                    except RuntimeError as exc:
                        self._error(503, str(exc))
            elif path == "/api/status":
                self._json(self.app.status())
            elif path == "/api/instruments":
                try:
                    self._json(self.app.instruments(query))
                except KeyError:
                    self._error(404, f"unknown market {query.get('market')}")
            elif path == "/api/spark":
                secids = [s for s in query.get("secids", "").split(",") if s][:60]
                points = min(240, max(5, _int(query.get("points"), 40)))
                self._json({"series": self.app.db.spark_series(secids, points)})
            elif path == "/api/activity":
                self._json({"events": self.app.db.recent_events(60),
                            "runs": self.app.db.recent_runs(20)})
            elif path == "/api/export":
                mid = query.get("market", "")
                try:
                    name, data = self.app.export_csv(mid)
                    self._send(200, data.encode("utf-8-sig"), "text/csv; charset=utf-8",
                               {"Content-Disposition": f'attachment; filename="{name}"'})
                except KeyError:
                    self._error(404, f"unknown market {mid}")
            elif path == "/api/health":
                self._json({"ok": True, "time": int(time.time())})
            elif path == "/api/debug/threads":
                import sys
                import traceback
                frames = sys._current_frames()
                out = []
                for thread in threading.enumerate():
                    frame = frames.get(thread.ident)
                    stack = [line.strip() for line in traceback.format_stack(frame)[-6:]] if frame else []
                    out.append({"thread": thread.name, "daemon": thread.daemon, "stack": stack})
                self._json({"threads": out})
            else:
                self._static(path.lstrip("/"))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("request failed: %s", path)
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = _int(self.headers.get("Content-Length"), 0)
        body = self.rfile.read(length) if length else b""
        if parsed.path.startswith("/api/v1/"):
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            self._v1(parsed.path, query, body)
            return
        if parsed.path == "/api/refresh":
            payload = {}
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except ValueError:
                    payload = {}
            try:
                self._json(self.app.trigger(payload.get("market")), 202)
            except KeyError as exc:
                self._error(404, f"unknown market {exc}")
            except RuntimeError as exc:
                self._error(503, str(exc))
        elif parsed.path == "/api/signals/refresh":
            if self.app.hub is None:
                self._error(503, "signal hub not attached")
                return
            threading.Thread(target=self.app.hub.run_due, kwargs={"force": True},
                             name="signals-refresh", daemon=True).start()
            self._json({"accepted": True}, 202)
        elif parsed.path == "/api/watchlist":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except ValueError:
                payload = {}
            secid = (payload.get("secid") or "").strip()
            if not secid:
                self._error(400, "secid required")
                return
            secid = self.app.resolve(secid)
            row = self.app.db.instrument(secid) or {}
            name = payload.get("name") or row.get("name") or secid
            self.app.db.watch_add(secid, name)
            self._json({"added": secid, "name": name, **self.app.watchlist()})
        else:
            self._error(404, "not found")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/watchlist/"):
            secid = urllib.parse.unquote(parsed.path[len("/api/watchlist/"):])
            removed = self.app.db.watch_remove(secid)
            self._json({"removed": removed, **self.app.watchlist()})
        else:
            self._error(404, "not found")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def build_server(monitor: Monitor, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": monitor})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(monitor: Monitor, host: str, port: int) -> None:
    httpd = build_server(monitor, host, port)
    log.info("serving on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
