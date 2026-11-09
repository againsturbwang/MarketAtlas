# T+1 evidence protocol

A contract for handing EastMoney data to an LLM that has to predict next-session
performance. Two ways in — plain HTTP for any language, MCP for any agent host —
over the same data and the same rules.

---

## The idea

The model should not read your database. It should receive a small, ranked,
self-describing packet of evidence, plus the ids it needs to dig further. Three
rules make that work:

1. **Point in time.** Every request takes `as_of`. Nothing published after that
   instant appears in the response. Default is now, anchored to the last session
   close, so an evening request sees the 17:30 dragon-tiger list while a 15:00
   replay does not.
2. **Self-describing.** Numbers carry units, facts carry source ids, and missing
   data appears in `caveats` instead of being silently substituted.
3. **Bounded.** Each tool returns roughly what one reasoning step needs, not the
   whole store.

The two effects that matter: a backtest cannot accidentally peek at tomorrow, and
a live call cannot be blamed for data that had not been published yet.

---

## Quick start — HTTP

```powershell
# what is knowable right now
curl http://127.0.0.1:8787/api/v1/meta
curl http://127.0.0.1:8787/api/v1/regime
curl "http://127.0.0.1:8787/api/v1/candidates?limit=10"
curl "http://127.0.0.1:8787/api/v1/stock/0.002131/evidence"

# replay a cutoff
curl "http://127.0.0.1:8787/api/v1/regime?as_of=2026-11-05T15:00"
```

OpenAPI 3.1 lives at `/api/v1/openapi.json` — feed it straight into a function-
calling model that accepts tool schemas.

## Quick start — MCP

```powershell
python mcp_server.py --selftest      # tool manifest + live check
python mcp_server.py                 # stdio transport
```

Host config:

```json
{"mcpServers": {"eastmoney": {
  "command": "python",
  "args": ["A:\\Scraper\\EastMoney\\mcp_server.py"],
  "env": {"EM_BASE_URL": "http://127.0.0.1:8787"}}}}
```

Eight tools: `market_regime`, `screen_candidates`, `stock_evidence`,
`search_news`, `read_article`, `list_signals`, `report_prediction`, `scorecard`.

## Quick start — no tool calling

```
GET /api/v1/brief?as_of=2026-11-05
```

Returns one markdown document — indices, breadth, limit-up pool, sector flows,
top candidates with reasons — ready to paste into any chat model.

---

## Packet shape

```json
{
  "schema": "em.evidence/1",
  "kind": "stock_evidence",
  "as_of": "2026-11-05T18:30:00+08:00",
  "trade_date": "2026-11-05",
  "generated_at": "2026-11-05T18:31:02+08:00",
  "units": {"amount": "元 (CNY)", "pct": "percent, 1.0 = 1%",
            "price": "元", "volume": "手", "color": "red = up, green = down"},
  "facts": [
    {"kind": "price", "date": "2026-11-05", "close": 4.39, "pct": 10.03,
     "volume": 2147856345, "source": "kline"},
    {"kind": "momentum", "ret_1d": 10.03, "ret_5d": 18.4, "ret_20d": 41.2,
     "from_high_pct": 0.0, "source": "kline"},
    {"kind": "signal:dragon_tiger", "items": [{"day": "2026-11-05",
      "score": 329947550.87, "payload": {"net_buy": 329947550.87,
      "reason": "日涨幅偏离值达到7%的前5只证券"}}]}
  ],
  "caveats": ["融资融券截至 2026-11-04（交易所收盘后发布）"],
  "sources": [{"id": "kline", "label": "东方财富日K"}]
}
```

`facts[].kind` is stable: `indices`, `breadth`, `limit_up_pool`,
`limit_down_pool`, `sector_flow_top`, `sector_flow_bottom`, `connect`, `margin`,
`news_volume`, `price`, `momentum`, `news`, `signal:<name>`.

---

## The prediction loop

The platform is also the scorekeeper. Write a call back, and it gets graded
against the next daily bar:

```json
POST /api/v1/predictions
{
  "secid": "0.002131",
  "direction": "up",
  "confidence": 0.62,
  "expected_return_pct": 2.5,
  "as_of": "2026-11-05T18:30",
  "rationale": "龙虎榜净买 3.3亿 + 主力净占比 42.5%，题材为通用设备",
  "evidence": ["sig:dragon_tiger", "sig:stock_flow", "art:AN202611051831451253"],
  "model": "deepseek-v4"
}
```

Scoring runs automatically (or on `POST /api/v1/predictions/score`): the platform
takes the close on `as_of_date`, the close on the next available daily bar, and
records `realized_pct`, `actual`, `hit`, `brier`. A move inside ±0.5 % counts as
`flat`, so `direction: up` is only credited on a move that is actually tradable.

```
GET /api/v1/predictions/scorecard?days=30
{"scored": 42, "hit_rate": 0.55, "brier": 0.24,
 "by_direction": {"up": {"n": 24, "hit_rate": 0.58, "avg_realized": 1.31}},
 "calibration": {"0.6-0.8": {"n": 15, "hit_rate": 0.60}}}
```

Calibration is the number to watch. A model that says 0.8 and hits 55 % is
overconfident regardless of its hit rate.

---

## Worked example

```
1. market_regime()                    -> 涨停 84 家, 最高 4 板, 半导体流入 61亿
2. screen_candidates(limit=10)        -> 利欧股份 84.0 (涨停+龙虎榜+主力+人气)
3. stock_evidence("0.002131")         -> 封单 1.37亿, 主力净占比 42.5%, 人气 #7
4. search_news(q="通用设备")           -> 3 条相关报道
5. report_prediction(secid=..., direction="up", confidence=0.6, ...)
```

Five calls, roughly 4k tokens, and every claim traceable to a source id.

---

## Rules worth knowing

- `as_of` accepts `YYYY-MM-DD` (meaning 15:00 that day) or a full ISO datetime.
- Signal visibility uses the **publication** hour per feed, not our collection
  time: pools and flows at 15:00, dragon-tiger 17:30, margin and research 18:00.
- `screen_candidates` ranks *attention*, not expected return. Its `method` field
  states the formula and `reasons[]` shows every component.
- Daily bars are fetched on demand for any stock you ask about, so evidence and
  scoring work beyond the pre-collected watch set.
- Everything is pruned at seven days; `as_of` older than that returns empty
  packets rather than stale ones.
