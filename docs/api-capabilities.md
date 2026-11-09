# Capability check: enumeration, time and symbol queries

Generated 2026-11-06 by `scripts/check_capabilities.py` against the running
server (`http://127.0.0.1:8787`).

| Question | Answer | Endpoint |
| --- | --- | --- |
| Every market? | **Yes** — 48 markets in 10 groups, each with member count and live session state | `GET /api/markets` |
| Every stock code? | **Yes** — 29,834 unique symbols, 30 pages at 1,000/page, filterable | `GET /api/instruments` |
| By time? | **Yes** — point-in-time `as_of`, per-day signals, day windows, and explicit bar ranges | see below |
| By stock code? | **Yes** — snapshot, bars, flow, signals, evidence | see below |
| Pure backend? | **Yes** — zero non-standard-library imports, no LLM client, no inference call | — |

---

## 1. All markets

```
GET /api/markets
{"markets": 48, "memberships": 50357, "unique_instruments": 29834,
 "groups": [{"id": "global.asia", "name": "亚洲股市", "markets": [
   {"id": "global.asia", "name": "亚洲股市", "kind": "index",
    "interval": 45, "count": 21,
    "session": {"open": true, "phase": "open", "text": "交易中",
                "close_in": 1468, "close_at": 1793932200}}]}]}
```

`memberships` counts (market, symbol) pairs; `unique_instruments` counts
distinct symbols, because one symbol can sit in several markets.

## 2. All stock codes

```
GET /api/instruments?page=1&size=1000
{"page": 1, "size": 1000, "pages": 30, "total": 29834,
 "rows": [{"secid": "0.399317", "code": "399317", "name": "国证Ａ指",
           "kind": "index", "mkt": 0, "price": 6034.92, "pct": -1.48,
           "amount": 670384909072.76, "mcap": 97304847280146.0,
           "updated_at": 1793930566}],
 "next": "/api/instruments?page=2&size=1000"}
```

- `size` caps at 1000, so the whole universe is 30 requests.
- `page=2` returned a disjoint set (2,000 distinct secids across two pages).
- Filters: `market=` (e.g. `cn.a` → 5,926), `kind=index` (→ 1,649),
  `q=` (code / name / secid substring), `sort=amount|pct|price|code`.
- Per-market enumeration still works too: `/api/market/cn.a?page=1&size=500`
  returns `total`, `pages`, and page 12 correctly returns the last 426 rows.

## 3. Queries by time

| Granularity | Endpoint | Example |
| --- | --- | --- |
| Any instant (point-in-time) | `/api/v1/{regime,candidates,stock/{id}/evidence,news/search}` | `?as_of=2026-11-05T15:00` |
| Whole trading day (signals) | `/api/signals/{source}` | `?day=2026-11-05` |
| Rolling window (news) | `/api/intel/articles`, `/api/intel/digest` | `?days=1` |
| Explicit bar range | `/api/stock/{secid}/kline` | `?from=2026-09-01&to=2026-09-30` |

Verified: `as_of=2026-11-05T15:00` returns 8 regime facts; a kline range query
returned 21 bars spanning exactly 2026-09-01 → 2026-09-30, not the last N bars.

Limits worth knowing: the news window is capped at 7 days (the retention period),
and quote history is not exposed as a range — only the current snapshot and
daily bars.

## 4. Queries by stock code

| Endpoint | Returns |
| --- | --- |
| `/api/stock/{secid}` | live snapshot: price, book, trades, trends, flow |
| `/api/stock/{secid}/kline?klt=101\|102\|103` | daily / weekly / monthly bars |
| `/api/stock/{secid}/flow` | daily capital flow |
| `/api/signals/stock/{secid}` | every T+1 signal for the name |
| `/api/v1/stock/{secid}/evidence` | point-in-time evidence packet |
| `/api/search?q=` | resolve a code or name to a secid |

`secid` accepts `1.600519`, `600519` or `sh600519`.

## 5. Pure backend

- **15 local modules, no third-party imports.** A dependency walk over every
  file reports `non-stdlib imports: none`.
- `requirements.txt` lists only `tzdata`, which supplies the IANA timezone
  database that CPython omits on Windows — data, not code.
- **No LLM client and no inference call**: no `openai`, `anthropic`,
  `langchain`, `transformers`, `httpx` or `requests` anywhere.
- Outbound hosts are exactly: `push2.eastmoney.com` and other
  `*.eastmoney.com` endpoints, the proxy provider `dmdaili.com`, and two
  time-sync sources (`cloudflare.com`, `timeapi.io`).
- Everything analytical is deterministic: the candidate score is a published
  weighted formula, the evidence packets are queries, the scorecard is
  arithmetic. The only "judgement" in the system is whatever the connected LLM
  adds on top.

## Known gaps

- `/api/market/{id}` still reports `pages` only after this change — older
  clients that computed `ceil(total/size)` themselves are unaffected.
- Quote history is not queryable by range; only the latest snapshot and daily
  kline bars are.
- `/api/instruments` reads `quote_latest`, so a symbol that has never been
  collected will not appear until its market has been scraped once.
