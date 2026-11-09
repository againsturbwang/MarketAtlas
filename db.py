"""SQLite storage layer: schema, bulk upserts, query helpers.

One writer connection guarded by a lock (sqlite3 is happiest that way) plus
per-thread reader connections, all in WAL mode so the API can serve while the
collector is writing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=8000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS markets (
    id         TEXT PRIMARY KEY,
    grp        TEXT NOT NULL,
    name       TEXT NOT NULL,
    fs         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    interval   INTEGER NOT NULL DEFAULT 120,
    history    INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS instruments (
    secid      TEXT PRIMARY KEY,
    market_id  TEXT NOT NULL,
    mkt        INTEGER,
    code       TEXT,
    name       TEXT,
    kind       TEXT,
    first_seen INTEGER,
    last_seen  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_instruments_market ON instruments(market_id);

CREATE TABLE IF NOT EXISTS quote_latest (
    secid      TEXT PRIMARY KEY,
    mkt        INTEGER,
    code       TEXT,
    name       TEXT,
    kind       TEXT,
    price      REAL,
    pct        REAL,
    chg        REAL,
    open       REAL,
    high       REAL,
    low        REAL,
    prev_close REAL,
    volume     REAL,
    amount     REAL,
    amplitude  REAL,
    turnover   REAL,
    pe         REAL,
    pb         REAL,
    mcap       REAL,
    extra      TEXT,
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_latest_name ON quote_latest(name);

-- instruments can belong to several markets (regional basket + asset class)
CREATE TABLE IF NOT EXISTS market_members (
    market_id TEXT NOT NULL,
    secid     TEXT NOT NULL,
    PRIMARY KEY (market_id, secid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_members_secid ON market_members(secid);

CREATE TABLE IF NOT EXISTS quotes (
    secid     TEXT NOT NULL,
    market_id TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    price     REAL,
    pct       REAL,
    chg       REAL,
    volume    REAL,
    amount    REAL,
    high      REAL,
    low       REAL,
    open      REAL,
    prev_close REAL,
    PRIMARY KEY (secid, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_quotes_market ON quotes(market_id, ts);

CREATE TABLE IF NOT EXISTS market_stats (
    market_id  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    total      INTEGER,
    up         INTEGER,
    down       INTEGER,
    flat       INTEGER,
    limit_up   INTEGER,
    limit_down INTEGER,
    avg_pct    REAL,
    amount     REAL,
    top_gain   TEXT,
    top_loss   TEXT,
    PRIMARY KEY (market_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS kline (
    secid  TEXT NOT NULL,
    klt    INTEGER NOT NULL,
    d      TEXT NOT NULL,
    open   REAL,
    close  REAL,
    high   REAL,
    low    REAL,
    volume REAL,
    amount REAL,
    pct    REAL,
    PRIMARY KEY (secid, klt, d)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started  INTEGER,
    finished INTEGER,
    ok       INTEGER,
    markets  INTEGER,
    requests INTEGER,
    rows     INTEGER,
    errors   INTEGER,
    duration REAL,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS collect_state (
    market_id  TEXT PRIMARY KEY,
    last_ts    INTEGER,
    last_ok    INTEGER,
    last_error TEXT,
    rows       INTEGER,
    duration   REAL,
    total      INTEGER
);

CREATE TABLE IF NOT EXISTS proxies (
    ip         TEXT NOT NULL,
    port       INTEGER NOT NULL,
    scheme     TEXT NOT NULL DEFAULT 'http',
    fetched_at INTEGER,
    expires_at INTEGER,
    ok         INTEGER DEFAULT 0,
    fail       INTEGER DEFAULT 0,
    last_used  INTEGER,
    last_error TEXT,
    PRIMARY KEY (ip, port)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    scope   TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- short-lived cache for on-demand per-stock payloads (book, flow, kline, ticks)
CREATE TABLE IF NOT EXISTS stock_cache (
    secid      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT,
    updated_at INTEGER,
    PRIMARY KEY (secid, kind)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS watchlist (
    secid    TEXT PRIMARY KEY,
    name     TEXT,
    added_at INTEGER,
    position INTEGER
);

-- T+1 intelligence scraped from EastMoney (kept for 7 days)
CREATE TABLE IF NOT EXISTS signals (
    source  TEXT NOT NULL,
    day     TEXT NOT NULL,
    key     TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    stock   TEXT,
    name    TEXT,
    kind    TEXT,
    score   REAL,
    payload TEXT,
    PRIMARY KEY (source, day, key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_signals_day ON signals(day);
CREATE INDEX IF NOT EXISTS idx_signals_stock ON signals(stock);

CREATE TABLE IF NOT EXISTS signal_state (
    source   TEXT PRIMARY KEY,
    last_ts  INTEGER,
    last_ok  INTEGER,
    rows     INTEGER,
    error    TEXT
);

-- full-text news / research / community items with 7-day retention
CREATE TABLE IF NOT EXISTS articles (
    art_id    TEXT PRIMARY KEY,
    section   TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    published INTEGER,
    title     TEXT,
    summary   TEXT,
    body      TEXT,
    url       TEXT,
    media     TEXT,
    stocks    TEXT,
    words     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_articles_section ON articles(section, published);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published);

-- predictions written back by an LLM agent, scored against realised returns
CREATE TABLE IF NOT EXISTS predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          INTEGER NOT NULL,
    as_of               TEXT NOT NULL,
    as_of_date          TEXT NOT NULL,
    secid               TEXT NOT NULL,
    name                TEXT,
    direction           TEXT NOT NULL,
    confidence          REAL,
    expected_return_pct REAL,
    horizon_days        INTEGER DEFAULT 1,
    rationale           TEXT,
    evidence            TEXT,
    model               TEXT,
    scored_at           INTEGER,
    base_date           TEXT,
    base_close          REAL,
    result_date         TEXT,
    result_close        REAL,
    realized_pct        REAL,
    actual              TEXT,
    hit                 INTEGER,
    brier               REAL
);
CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(as_of_date);
CREATE INDEX IF NOT EXISTS idx_pred_unscored ON predictions(scored_at);
"""

SORTABLE = {
    "pct": "pct",
    "price": "price",
    "chg": "chg",
    "amount": "amount",
    "volume": "volume",
    "code": "code",
    "name": "name",
    "turnover": "turnover",
    "mcap": "mcap",
}


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self._write.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._local = threading.local()
        with self._lock:
            self._write.executescript(SCHEMA)
            # migration: early signal rows had no `stock` column
            cols = {r[1] for r in self._write.execute("PRAGMA table_info(signals)")}
            if cols and "stock" not in cols:
                self._write.executescript(
                    "DROP TABLE signals;"
                    "CREATE TABLE signals (source TEXT NOT NULL, day TEXT NOT NULL,"
                    " key TEXT NOT NULL, ts INTEGER NOT NULL, stock TEXT, name TEXT,"
                    " kind TEXT, score REAL, payload TEXT, PRIMARY KEY (source, day, key)) WITHOUT ROWID;"
                    "CREATE INDEX idx_signals_day ON signals(day);"
                    "CREATE INDEX idx_signals_stock ON signals(stock);")
            self._write.commit()

    # -- connections -----------------------------------------------------
    def reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            self._write.close()

    # -- seeding ---------------------------------------------------------
    def sync_markets(self, markets: Iterable[Any]) -> None:
        rows = [
            (m.id, m.group, m.name, m.fs, m.kind, m.interval, int(bool(m.history)), i)
            for i, m in enumerate(markets)
        ]
        with self._lock, self._write:
            self._write.executemany(
                """INSERT INTO markets (id, grp, name, fs, kind, interval, history, position)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     grp=excluded.grp, name=excluded.name, fs=excluded.fs,
                     kind=excluded.kind, interval=excluded.interval,
                     history=excluded.history, position=excluded.position""",
                rows,
            )

    # -- writes ----------------------------------------------------------
    def upsert_market_rows(self, market_id: str, kind: str, rows: Sequence[dict], ts: int,
                           keep_history: bool) -> int:
        """Write one market's quote page set. Returns the number of rows stored."""
        if not rows:
            return 0
        latest = []
        hist = []
        instruments = []
        members = []
        for r in rows:
            secid = r["secid"]
            extra = json.dumps(r.get("extra") or {}, ensure_ascii=False, separators=(",", ":"))
            base = (
                secid, r.get("mkt"), r.get("code"), r.get("name"), kind,
                r.get("price"), r.get("pct"), r.get("chg"), r.get("open"), r.get("high"),
                r.get("low"), r.get("prev_close"), r.get("volume"), r.get("amount"),
                r.get("amplitude"), r.get("turnover"), r.get("pe"), r.get("pb"),
                r.get("mcap"),
            )
            latest.append(base + (extra, ts))
            members.append((market_id, secid))
            instruments.append((secid, market_id, r.get("mkt"), r.get("code"), r.get("name"),
                                kind, ts, ts))
            if keep_history:
                hist.append((secid, market_id, ts, r.get("price"), r.get("pct"), r.get("chg"),
                             r.get("volume"), r.get("amount"), r.get("high"), r.get("low"),
                             r.get("open"), r.get("prev_close")))
        with self._lock, self._write:
            self._write.executemany(
                """INSERT INTO quote_latest
                   (secid, mkt, code, name, kind, price, pct, chg, open, high, low,
                    prev_close, volume, amount, amplitude, turnover, pe, pb, mcap, extra, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(secid) DO UPDATE SET
                     mkt=excluded.mkt, code=excluded.code,
                     name=excluded.name, kind=excluded.kind, price=excluded.price,
                     pct=excluded.pct, chg=excluded.chg, open=excluded.open,
                     high=excluded.high, low=excluded.low, prev_close=excluded.prev_close,
                     volume=excluded.volume, amount=excluded.amount,
                     amplitude=excluded.amplitude, turnover=excluded.turnover,
                     pe=excluded.pe, pb=excluded.pb, mcap=excluded.mcap,
                     extra=excluded.extra, updated_at=excluded.updated_at""",
                latest,
            )
            self._write.executemany(
                "INSERT OR IGNORE INTO market_members (market_id, secid) VALUES (?,?)",
                members,
            )
            self._write.executemany(
                """INSERT INTO instruments (secid, market_id, mkt, code, name, kind, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(secid) DO UPDATE SET
                     market_id=excluded.market_id, name=excluded.name,
                     kind=excluded.kind, last_seen=excluded.last_seen""",
                instruments,
            )
            if hist:
                self._write.executemany(
                    """INSERT OR REPLACE INTO quotes
                       (secid, market_id, ts, price, pct, chg, volume, amount, high, low, open, prev_close)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    hist,
                )
        return len(latest)

    def record_stats(self, market_id: str, ts: int, stats: dict) -> None:
        with self._lock, self._write:
            self._write.execute(
                """INSERT OR REPLACE INTO market_stats
                   (market_id, ts, total, up, down, flat, limit_up, limit_down, avg_pct,
                    amount, top_gain, top_loss)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (market_id, ts, stats.get("total", 0), stats.get("up", 0), stats.get("down", 0),
                 stats.get("flat", 0), stats.get("limit_up", 0), stats.get("limit_down", 0),
                 stats.get("avg_pct", 0.0), stats.get("amount", 0.0),
                 json.dumps(stats.get("top_gain"), ensure_ascii=False),
                 json.dumps(stats.get("top_loss"), ensure_ascii=False)),
            )

    def record_state(self, market_id: str, ts: int, ok: bool, error: str | None,
                     rows: int, duration: float, total: int) -> None:
        with self._lock, self._write:
            self._write.execute(
                """INSERT INTO collect_state (market_id, last_ts, last_ok, last_error, rows, duration, total)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(market_id) DO UPDATE SET
                     last_ts=excluded.last_ts, last_ok=excluded.last_ok,
                     last_error=excluded.last_error, rows=excluded.rows,
                     duration=excluded.duration, total=excluded.total""",
                (market_id, ts, int(ok), error, rows, duration, total),
            )

    def start_run(self, started: int) -> int:
        with self._lock, self._write:
            cur = self._write.execute("INSERT INTO runs (started) VALUES (?)", (started,))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, ok: bool, markets: int, requests: int, rows: int,
                   errors: int, duration: float, detail: str = "") -> None:
        with self._lock, self._write:
            self._write.execute(
                """UPDATE runs SET finished=?, ok=?, markets=?, requests=?, rows=?, errors=?,
                   duration=?, detail=? WHERE id=?""",
                (int(time.time()), int(ok), markets, requests, rows, errors,
                 round(duration, 3), detail[:2000], run_id),
            )

    def save_kline(self, secid: str, klt: int, bars: Sequence[tuple]) -> int:
        if not bars:
            return 0
        with self._lock, self._write:
            self._write.executemany(
                """INSERT OR REPLACE INTO kline (secid, klt, d, open, close, high, low, volume, amount, pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(secid, klt, *b) for b in bars],
            )
        return len(bars)

    def save_proxies(self, proxies: Sequence[dict]) -> None:
        if not proxies:
            return
        with self._lock, self._write:
            self._write.executemany(
                """INSERT INTO proxies (ip, port, scheme, fetched_at, expires_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(ip, port) DO UPDATE SET
                     fetched_at=excluded.fetched_at, expires_at=excluded.expires_at""",
                [(p["ip"], p["port"], p.get("scheme", "http"), p["fetched_at"], p["expires_at"])
                 for p in proxies],
            )

    def report_proxy(self, ip: str, port: int, ok: bool, error: str | None = None) -> None:
        with self._lock, self._write:
            if ok:
                self._write.execute(
                    "UPDATE proxies SET ok=ok+1, last_used=? WHERE ip=? AND port=?",
                    (int(time.time()), ip, port))
            else:
                self._write.execute(
                    "UPDATE proxies SET fail=fail+1, last_used=?, last_error=? WHERE ip=? AND port=?",
                    (int(time.time()), (error or "")[:200], ip, port))

    def log_event(self, level: str, scope: str, message: str) -> None:
        with self._lock, self._write:
            self._write.execute("INSERT INTO events (ts, level, scope, message) VALUES (?,?,?,?)",
                                (int(time.time()), level, scope, message[:500]))

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._write:
            self._write.execute(
                "INSERT INTO meta (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value))

    # -- maintenance -----------------------------------------------------
    def prune(self, retention_hours: int, signal_days: int = 7) -> dict[str, int]:
        cutoff = int(time.time() - retention_hours * 3600)
        stats_cutoff = int(time.time() - retention_hours * 3600)
        removed = {}
        with self._lock, self._write:
            sig_cutoff = time.strftime("%Y-%m-%d",
                                       time.localtime(time.time() - signal_days * 86400))
            cur = self._write.execute("DELETE FROM signals WHERE day < ?", (sig_cutoff,))
            removed["signals"] = cur.rowcount
            cur = self._write.execute("DELETE FROM quotes WHERE ts < ?", (cutoff,))
            removed["quotes"] = cur.rowcount
            cur = self._write.execute("DELETE FROM market_stats WHERE ts < ?", (stats_cutoff,))
            removed["market_stats"] = cur.rowcount
            cur = self._write.execute("DELETE FROM events WHERE id NOT IN "
                                      "(SELECT id FROM events ORDER BY id DESC LIMIT 800)")
            removed["events"] = cur.rowcount
            cur = self._write.execute("DELETE FROM runs WHERE id NOT IN "
                                      "(SELECT id FROM runs ORDER BY id DESC LIMIT 200)")
            removed["runs"] = cur.rowcount
            cur = self._write.execute("DELETE FROM proxies WHERE fetched_at < ?",
                                      (int(time.time() - 86400),))
            removed["proxies"] = cur.rowcount
        return removed

    # -- reads -----------------------------------------------------------
    def markets(self) -> list[dict]:
        rows = self.reader().execute("SELECT * FROM markets ORDER BY position").fetchall()
        return [dict(r) for r in rows]

    def market_stats_latest(self, limit_per_market: int = 1) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.reader().execute(
            """SELECT s.* FROM market_stats s
               JOIN (SELECT market_id, MAX(ts) AS ts FROM market_stats GROUP BY market_id) m
                 ON s.market_id = m.market_id AND s.ts = m.ts"""):
            d = dict(r)
            for key in ("top_gain", "top_loss"):
                if d.get(key):
                    try:
                        d[key] = json.loads(d[key])
                    except (TypeError, ValueError):
                        d[key] = None
            out[d["market_id"]] = d
        return out

    def collect_states(self) -> dict[str, dict]:
        return {r["market_id"]: dict(r) for r in self.reader().execute("SELECT * FROM collect_state")}

    def rows_for_market(self, market_id: str, *, page: int, size: int, sort: str = "pct",
                        order: str = "desc", search: str = "") -> tuple[list[dict], int]:
        col = SORTABLE.get(sort, "pct")
        direction = "ASC" if order.lower() == "asc" else "DESC"
        where = "m.market_id = ?"
        params: list[Any] = [market_id]
        if search:
            where += " AND (q.name LIKE ? OR q.code LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like])
        conn = self.reader()
        total = conn.execute(
            f"SELECT COUNT(*) FROM market_members m JOIN quote_latest q ON q.secid = m.secid "
            f"WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT q.secid, m.market_id AS market_id, q.mkt, q.code, q.name, q.kind,
                       q.price, q.pct, q.chg, q.open, q.high, q.low, q.prev_close, q.volume,
                       q.amount, q.amplitude, q.turnover, q.pe, q.pb, q.mcap, q.updated_at
                FROM market_members m JOIN quote_latest q ON q.secid = m.secid
                WHERE {where}
                ORDER BY q.{col} IS NULL, q.{col} {direction}, q.code ASC
                LIMIT ? OFFSET ?""",
            params + [size, (page - 1) * size],
        ).fetchall()
        return [dict(r) for r in rows], int(total)

    def market_index_quotes(self, market_ids: Sequence[str]) -> list[dict]:
        if not market_ids:
            return []
        marks = ",".join("?" * len(market_ids))
        return [dict(r) for r in self.reader().execute(
            f"""SELECT q.*, m.market_id FROM market_members m
                JOIN quote_latest q ON q.secid = m.secid
                WHERE m.market_id IN ({marks})""", list(market_ids))]

    def by_secids(self, secids: Sequence[str]) -> list[dict]:
        if not secids:
            return []
        marks = ",".join("?" * len(secids))
        return [dict(r) for r in self.reader().execute(
            f"SELECT * FROM quote_latest WHERE secid IN ({marks})", list(secids))]

    def movers(self, limit: int = 20, market_ids: Sequence[str] | None = None,
               kind: str | None = None) -> dict[str, list[dict]]:
        conn = self.reader()
        clauses = ["q.price IS NOT NULL", "q.pct IS NOT NULL"]
        params: list[Any] = []
        if market_ids:
            clauses.append("q.secid IN (SELECT secid FROM market_members WHERE market_id IN ("
                           + ",".join("?" * len(market_ids)) + "))")
            params.extend(market_ids)
        if kind:
            clauses.append("q.kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        cols = """q.secid, q.code, q.name, q.kind, q.price, q.pct, q.chg, q.amount, q.volume"""
        gain = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM quote_latest q WHERE {where} ORDER BY q.pct DESC LIMIT ?",
            params + [limit])]
        loss = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM quote_latest q WHERE {where} ORDER BY q.pct ASC LIMIT ?",
            params + [limit])]
        active = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM quote_latest q WHERE {where} AND q.amount > 0 "
            f"ORDER BY q.amount DESC LIMIT ?", params + [limit])]
        return {"gainers": gain, "losers": loss, "active": active}

    def instrument(self, secid: str) -> dict | None:
        row = self.reader().execute(
            "SELECT * FROM quote_latest WHERE secid = ?", (secid,)).fetchone()
        if not row:
            return None
        out = dict(row)
        market = self.reader().execute(
            "SELECT market_id FROM market_members WHERE secid = ? LIMIT 1", (secid,)).fetchone()
        out["market_id"] = market["market_id"] if market else None
        return out

    def intraday(self, secid: str, minutes: int = 720) -> list[dict]:
        cutoff = int(time.time() - minutes * 60)
        rows = self.reader().execute(
            """SELECT ts, price, pct, volume FROM quotes
               WHERE secid = ? AND ts >= ? ORDER BY ts""", (secid, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def spark_series(self, secids: Sequence[str], points: int = 40) -> dict[str, list]:
        conn = self.reader()
        out: dict[str, list] = {}
        for secid in secids:
            rows = conn.execute(
                "SELECT ts, price FROM quotes WHERE secid = ? AND price IS NOT NULL "
                "ORDER BY ts DESC LIMIT ?", (secid, points)).fetchall()
            if rows:
                out[secid] = [[r["ts"], r["price"]] for r in reversed(rows)]
        return out

    def kline_range(self, secid: str, start: str, end: str, klt: int = 101,
                    limit: int = 1000) -> list[dict]:
        rows = self.reader().execute(
            """SELECT d, open, close, high, low, volume, amount, pct FROM kline
               WHERE secid = ? AND klt = ? AND d >= ? AND d <= ?
               ORDER BY d ASC LIMIT ?""",
            (secid, klt, start, end, limit)).fetchall()
        return [dict(r) for r in rows]

    def instruments_page(self, *, page: int = 1, size: int = 500, market: str | None = None,
                         kind: str | None = None, q: str | None = None,
                         sort: str = "amount") -> tuple[list[dict], int]:
        """Flat enumeration of the whole symbol universe."""
        where, params = [], []
        join = ""
        if market:
            join = " JOIN market_members m ON m.secid = q.secid AND m.market_id = ? "
            params.append(market)
        if kind:
            where.append("q.kind = ?")
            params.append(kind)
        if q:
            where.append("(q.code LIKE ? OR q.name LIKE ? OR q.secid LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        conn = self.reader()
        total = conn.execute(
            f"SELECT COUNT(*) FROM quote_latest q{join}{clause}", params).fetchone()[0]
        col = "q." + (SORTABLE.get(sort, "amount"))
        rows = conn.execute(
            f"""SELECT q.secid, q.code, q.name, q.kind, q.mkt, q.price, q.pct, q.chg,
                       q.amount, q.turnover, q.mcap, q.updated_at
                FROM quote_latest q{join}{clause}
                ORDER BY {col} DESC LIMIT ? OFFSET ?""",
            params + [size, (page - 1) * size]).fetchall()
        return [dict(r) for r in rows], total

    def instrument_count(self) -> int:
        return self.reader().execute("SELECT COUNT(*) FROM quote_latest").fetchone()[0]

    def market_counts(self) -> dict[str, int]:
        return {r["market_id"]: r["n"] for r in self.reader().execute(
            "SELECT market_id, COUNT(*) n FROM market_members GROUP BY market_id")}

    def kline(self, secid: str, klt: int = 101, limit: int = 120) -> list[dict]:
        rows = self.reader().execute(
            "SELECT d, open, close, high, low, volume, amount, pct FROM kline "
            "WHERE secid = ? AND klt = ? ORDER BY d DESC LIMIT ?",
            (secid, klt, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_runs(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.reader().execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def recent_events(self, limit: int = 40) -> list[dict]:
        return [dict(r) for r in self.reader().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def proxy_summary(self) -> dict:
        row = self.reader().execute(
            """SELECT COUNT(*) AS seen, COALESCE(SUM(ok),0) AS ok, COALESCE(SUM(fail),0) AS fail,
                      MAX(last_used) AS last_used FROM proxies""").fetchone()
        live = self.reader().execute(
            "SELECT COUNT(*) FROM proxies WHERE expires_at > ?", (int(time.time()),)).fetchone()[0]
        out = dict(row)
        out["live"] = live
        return out

    def counts(self) -> dict:
        conn = self.reader()
        return {
            "instruments": conn.execute("SELECT COUNT(*) FROM quote_latest").fetchone()[0],
            "history_points": conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
            "markets": conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        }

    def stats_history(self, market_id: str, limit: int = 240) -> list[dict]:
        rows = self.reader().execute(
            "SELECT ts, avg_pct, up, down, total FROM market_stats WHERE market_id = ? "
            "ORDER BY ts DESC LIMIT ?", (market_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # -- per-instrument cache --------------------------------------------
    def cache_get(self, secid: str, kind: str, ttl: int) -> Any | None:
        row = self.reader().execute(
            "SELECT payload, updated_at FROM stock_cache WHERE secid = ? AND kind = ?",
            (secid, kind)).fetchone()
        if not row:
            return None
        if time.time() - (row["updated_at"] or 0) > ttl:
            return None
        try:
            return json.loads(row["payload"])
        except (TypeError, ValueError):
            return None

    def cache_put(self, secid: str, kind: str, payload: Any) -> None:
        with self._lock, self._write:
            self._write.execute(
                """INSERT INTO stock_cache (secid, kind, payload, updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(secid, kind) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (secid, kind, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                 int(time.time())))

    # -- watchlist --------------------------------------------------------
    def watchlist(self) -> list[dict]:
        return [dict(r) for r in self.reader().execute(
            "SELECT * FROM watchlist ORDER BY position, added_at")]

    def watch_add(self, secid: str, name: str) -> None:
        with self._lock, self._write:
            self._write.execute(
                """INSERT INTO watchlist (secid, name, added_at, position) VALUES (?,?,?,?)
                   ON CONFLICT(secid) DO UPDATE SET name=excluded.name""",
                (secid, name, int(time.time()),
                 self._write.execute("SELECT COALESCE(MAX(position),0)+1 FROM watchlist"
                                     ).fetchone()[0]))

    def watch_remove(self, secid: str) -> int:
        with self._lock, self._write:
            cur = self._write.execute("DELETE FROM watchlist WHERE secid = ?", (secid,))
            return cur.rowcount

    def search(self, query: str, limit: int = 25) -> list[dict]:
        if not query:
            return []
        like = f"%{query}%"
        return [dict(r) for r in self.reader().execute(
            """SELECT secid, code, name, kind, price, pct, amount FROM quote_latest
               WHERE code = ? OR code LIKE ? OR name LIKE ?
               ORDER BY (code = ?) DESC, (code LIKE ?) DESC, amount DESC
               LIMIT ?""",
            (query, like, like, query, f"{query}%", limit))]

    # -- T+1 signals ------------------------------------------------------
    def upsert_signals(self, source: str, records: list[dict]) -> int:
        if not records:
            return 0
        now = int(time.time())
        rows = [(r["source"], r["day"], r["key"], now, r.get("stock"), r.get("name"),
                 r.get("kind"), r.get("score"),
                 json.dumps(r.get("payload") or {}, ensure_ascii=False, separators=(",", ":")))
                for r in records]
        with self._lock, self._write:
            self._write.executemany(
                """INSERT INTO signals (source, day, key, ts, stock, name, kind, score, payload)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, day, key) DO UPDATE SET
                     ts=excluded.ts, stock=excluded.stock, name=excluded.name,
                     kind=excluded.kind, score=excluded.score, payload=excluded.payload""",
                rows)
        return len(rows)

    def signal_state_put(self, source: str, ok: bool, rows: int, error: str | None) -> None:
        with self._lock, self._write:
            self._write.execute(
                """INSERT INTO signal_state (source, last_ts, last_ok, rows, error)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET
                     last_ts=excluded.last_ts, last_ok=excluded.last_ok,
                     rows=excluded.rows, error=excluded.error""",
                (source, int(time.time()), int(ok), rows, (error or "")[:300]))

    def signal_states(self) -> dict[str, dict]:
        return {r["source"]: dict(r) for r in self.reader().execute("SELECT * FROM signal_state")}

    def signals(self, source: str | None = None, day: str | None = None,
                limit: int = 50, order: str = "desc") -> list[dict]:
        clauses, params = [], []
        if source:
            clauses.append("s.source = ?")
            params.append(source)
        if day:
            clauses.append("s.day = ?")
            params.append(day)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        direction = "ASC" if order.lower() == "asc" else "DESC"
        rows = self.reader().execute(
            f"""SELECT s.source, s.day, s.key, s.ts, s.stock, s.name, s.kind, s.score,
                       s.payload, q.name AS live_name
                FROM signals s LEFT JOIN quote_latest q ON q.secid = s.stock
                {where} ORDER BY s.score {direction}, s.name LIMIT ?""",
            params + [limit]).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["name"] = item.get("live_name") or item.get("name") or item.get("key")
            item.pop("live_name", None)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            out.append(item)
        return out

    def signals_for(self, stock: str, limit: int = 40) -> list[dict]:
        rows = self.reader().execute(
            """SELECT s.source, s.day, s.key, s.ts, s.stock, s.name, s.kind, s.score,
                      s.payload, q.name AS live_name
               FROM signals s LEFT JOIN quote_latest q ON q.secid = s.stock
               WHERE s.stock = ? ORDER BY s.day DESC, s.source LIMIT ?""", (stock, limit)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["name"] = item.get("live_name") or item.get("name")
            item.pop("live_name", None)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            out.append(item)
        return out

    def signal_summary(self, day: str | None = None) -> dict:
        day = day or time.strftime("%Y-%m-%d")
        rows = self.reader().execute(
            """SELECT source, COUNT(*) n, MAX(ts) ts FROM signals
               WHERE day = ? GROUP BY source""", (day,)).fetchall()
        if not rows:
            # before the first collection of the day, show the last session instead
            latest = self.latest_signal_day()
            if latest:
                day = latest
                rows = self.reader().execute(
                    """SELECT source, COUNT(*) n, MAX(ts) ts FROM signals
                       WHERE day = ? GROUP BY source""", (day,)).fetchall()
        span = self.reader().execute(
            "SELECT MIN(day), MAX(day), COUNT(*) FROM signals").fetchone()
        return {"day": day,
                "sources": {r["source"]: {"count": r["n"], "updated_at": r["ts"]} for r in rows},
                "window": {"from": span[0], "to": span[1], "total": span[2]}}

    def latest_signal_day(self, source: str | None = None) -> str | None:
        if source:
            row = self.reader().execute(
                "SELECT MAX(day) FROM signals WHERE source = ?", (source,)).fetchone()
        else:
            row = self.reader().execute("SELECT MAX(day) FROM signals").fetchone()
        return row[0] if row and row[0] else None

    def prune_signals(self, days: int = 7) -> int:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        with self._lock, self._write:
            cur = self._write.execute("DELETE FROM signals WHERE day < ?", (cutoff,))
            return cur.rowcount

    # -- articles (full text, 7 days) -------------------------------------
    def article_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        rows = self.reader().execute(
            f"SELECT art_id FROM articles WHERE art_id IN ({marks})", ids).fetchall()
        return {r["art_id"] for r in rows}

    def upsert_articles(self, records: list[dict]) -> int:
        if not records:
            return 0
        rows = []
        for r in records:
            body = r.get("body") or ""
            rows.append((r["art_id"], r["section"], int(time.time()), int(r.get("published") or 0),
                         r.get("title"), r.get("summary"), body, r.get("url"), r.get("media"),
                         json.dumps(r.get("stocks") or [], ensure_ascii=False), len(body)))
        with self._lock, self._write:
            self._write.executemany(
                """INSERT INTO articles
                   (art_id, section, ts, published, title, summary, body, url, media, stocks, words)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(art_id) DO UPDATE SET
                     section=excluded.section,
                     published=CASE WHEN excluded.published > 0 THEN excluded.published ELSE articles.published END,
                     title=excluded.title, summary=excluded.summary,
                     body=CASE WHEN LENGTH(excluded.body) > LENGTH(COALESCE(articles.body,''))
                               THEN excluded.body ELSE articles.body END,
                     url=excluded.url, media=excluded.media, stocks=excluded.stocks,
                     words=excluded.words""",
                rows)
        return len(rows)

    def pending_articles(self, limit: int = 12, since_hours: int = 24) -> list[dict]:
        cutoff = int(time.time() - since_hours * 3600)
        return [dict(r) for r in self.reader().execute(
            """SELECT art_id, section, url, title FROM articles
               WHERE LENGTH(COALESCE(body,'')) = 0 AND published >= ?
                 AND section NOT IN ('topic', 'kuaixun', 'community')
               ORDER BY published DESC LIMIT ?""", (cutoff, limit))]

    def articles(self, section: str | None = None, days: int = 7, limit: int = 50,
                 with_body: bool = False, q: str | None = None) -> list[dict]:
        since = int(time.time() - days * 86400)
        clauses = ["published >= ?"]
        params: list[Any] = [since]
        if section:
            clauses.append("section = ?")
            params.append(section)
        if q:
            clauses.append("(title LIKE ? OR summary LIKE ? OR body LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        cols = "art_id, section, ts, published, title, summary, url, media, stocks, words"
        if with_body:
            cols += ", body"
        rows = self.reader().execute(
            f"SELECT {cols} FROM articles WHERE {' AND '.join(clauses)} "
            "ORDER BY published DESC LIMIT ?", params + [limit]).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["stocks"] = json.loads(item.get("stocks") or "[]")
            except (TypeError, ValueError):
                item["stocks"] = []
            out.append(item)
        return out

    def article(self, art_id: str) -> dict | None:
        row = self.reader().execute("SELECT * FROM articles WHERE art_id = ?", (art_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["stocks"] = json.loads(item.get("stocks") or "[]")
        except (TypeError, ValueError):
            item["stocks"] = []
        return item

    def article_summary(self) -> dict:
        since = int(time.time() - 7 * 86400)
        rows = self.reader().execute(
            """SELECT section, COUNT(*) n, SUM(CASE WHEN LENGTH(COALESCE(body,''))>0 THEN 1 ELSE 0 END) AS full_text,
                      MAX(published) latest
               FROM articles WHERE published >= ? GROUP BY section""", (since,)).fetchall()
        total = self.reader().execute(
            "SELECT COUNT(*) FROM articles WHERE published >= ?", (since,)).fetchone()[0]
        return {"sections": {r["section"]: {"count": r["n"], "full_text": r["full_text"],
                                           "latest": r["latest"]} for r in rows},
                "total": total}

    def prune_articles(self, days: int = 7) -> int:
        cutoff = int(time.time() - days * 86400)
        with self._lock, self._write:
            cur = self._write.execute("DELETE FROM articles WHERE published < ?", (cutoff,))
            return cur.rowcount

    # -- daily bars (point-in-time reads) ---------------------------------
    def kline_last(self, secid: str, on_or_before: str, klt: int = 101) -> dict | None:
        row = self.reader().execute(
            """SELECT secid, d, open, close, high, low, volume, amount, pct FROM kline
               WHERE secid = ? AND klt = ? AND d <= ? ORDER BY d DESC LIMIT 1""",
            (secid, klt, on_or_before)).fetchone()
        return dict(row) if row else None

    def kline_next(self, secid: str, after: str, klt: int = 101) -> dict | None:
        row = self.reader().execute(
            """SELECT secid, d, open, close, high, low, volume, amount, pct FROM kline
               WHERE secid = ? AND klt = ? AND d > ? ORDER BY d ASC LIMIT 1""",
            (secid, klt, after)).fetchone()
        return dict(row) if row else None

    def kline_recent(self, secid: str, on_or_before: str, limit: int = 20,
                     klt: int = 101) -> list[dict]:
        rows = self.reader().execute(
            """SELECT secid, d, open, close, high, low, volume, amount, pct FROM kline
               WHERE secid = ? AND klt = ? AND d <= ? ORDER BY d DESC LIMIT ?""",
            (secid, klt, on_or_before, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def kline_last_many(self, secids: list[str], on_or_before: str,
                        klt: int = 101) -> dict[str, dict]:
        if not secids:
            return {}
        marks = ",".join("?" * len(secids))
        rows = self.reader().execute(
            f"""SELECT secid, d, close, pct FROM kline
                WHERE klt = ? AND secid IN ({marks}) AND d <= ?
                ORDER BY secid, d""",
            [klt] + secids + [on_or_before]).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            out[row["secid"]] = dict(row)     # ascending, so the last write wins
        return out

    def stats_at(self, market_id: str, before_ts: int) -> dict | None:
        row = self.reader().execute(
            """SELECT * FROM market_stats WHERE market_id = ? AND ts <= ?
               ORDER BY ts DESC LIMIT 1""", (market_id, before_ts)).fetchone()
        return dict(row) if row else None

    def quote(self, secid: str) -> dict | None:
        row = self.reader().execute(
            "SELECT * FROM quote_latest WHERE secid = ?", (secid,)).fetchone()
        return dict(row) if row else None

    # -- predictions (agent write-back + scoring) -------------------------
    def prediction_add(self, row: dict) -> int:
        with self._lock, self._write:
            cur = self._write.execute(
                """INSERT INTO predictions
                   (created_at, as_of, as_of_date, secid, name, direction, confidence,
                    expected_return_pct, horizon_days, rationale, evidence, model)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(time.time()), row["as_of"], row["as_of_date"], row["secid"],
                 row.get("name"), row["direction"], row.get("confidence"),
                 row.get("expected_return_pct"), row.get("horizon_days", 1),
                 row.get("rationale"),
                 json.dumps(row.get("evidence") or [], ensure_ascii=False),
                 row.get("model")))
            return int(cur.lastrowid)

    def predictions(self, days: int = 30, limit: int = 100, model: str | None = None,
                    unscored_only: bool = False) -> list[dict]:
        clauses = ["as_of_date >= ?"]
        params: list[Any] = [time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))]
        if model:
            clauses.append("model = ?")
            params.append(model)
        if unscored_only:
            clauses.append("scored_at IS NULL")
        rows = self.reader().execute(
            f"SELECT * FROM predictions WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.get("evidence") or "[]")
            except (TypeError, ValueError):
                item["evidence"] = []
            out.append(item)
        return out

    def prediction_unscored(self, limit: int = 500) -> list[dict]:
        return [dict(r) for r in self.reader().execute(
            "SELECT * FROM predictions WHERE scored_at IS NULL ORDER BY id LIMIT ?", (limit,))]

    def prediction_score(self, pred_id: int, fields: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._write:
            self._write.execute(
                f"UPDATE predictions SET {sets}, scored_at = ? WHERE id = ?",
                list(fields.values()) + [int(time.time()), pred_id])

    def prediction_scorecard(self, days: int = 30) -> dict:
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        rows = [dict(r) for r in self.reader().execute(
            "SELECT * FROM predictions WHERE as_of_date >= ? AND scored_at IS NOT NULL",
            (since,))]
        if not rows:
            return {"days": days, "scored": 0, "pending": self._pending_count(since)}
        hits = sum(1 for r in rows if r["hit"])
        brier = sum((r["brier"] or 0) for r in rows) / len(rows)
        by_dir: dict[str, dict] = {}
        for r in rows:
            slot = by_dir.setdefault(r["direction"], {"n": 0, "hits": 0, "avg_realized": 0.0})
            slot["n"] += 1
            slot["hits"] += 1 if r["hit"] else 0
            slot["avg_realized"] += r["realized_pct"] or 0
        for slot in by_dir.values():
            slot["hit_rate"] = round(slot["hits"] / slot["n"], 3)
            slot["avg_realized"] = round(slot["avg_realized"] / slot["n"], 3)
        buckets = {"0.0-0.4": [0, 0], "0.4-0.6": [0, 0], "0.6-0.8": [0, 0], "0.8-1.0": [0, 0]}
        for r in rows:
            conf = r["confidence"] or 0
            key = ("0.0-0.4" if conf < 0.4 else "0.4-0.6" if conf < 0.6
                   else "0.6-0.8" if conf < 0.8 else "0.8-1.0")
            buckets[key][0] += 1
            buckets[key][1] += 1 if r["hit"] else 0
        calibration = {k: {"n": v[0], "hit_rate": round(v[1] / v[0], 3) if v[0] else None}
                       for k, v in buckets.items()}
        return {"days": days, "scored": len(rows),
                "hit_rate": round(hits / len(rows), 3),
                "brier": round(brier, 4),
                "avg_realized_pct": round(sum(r["realized_pct"] or 0 for r in rows) / len(rows), 3),
                "by_direction": by_dir,
                "calibration": calibration,
                "pending": self._pending_count(since),
                "models": sorted({r["model"] for r in rows if r["model"]})}

    def _pending_count(self, since: str) -> int:
        return self.reader().execute(
            "SELECT COUNT(*) FROM predictions WHERE as_of_date >= ? AND scored_at IS NULL",
            (since,)).fetchone()[0]
