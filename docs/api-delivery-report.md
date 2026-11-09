# What the local server delivers to an agent

Generated 2026-11-06 09:56 by `scripts/audit_delivery.py`. Base URL `http://127.0.0.1:8787`. `28/28` probed routes answered 200.

## Verdict

- **K-lines are correct.** 591 closed bars across 5 instruments (A-share, index, HK, US) and two periods were compared field by field against EastMoney itself: **0 mismatches** in open/high/low/close/volume/amount/pct.
- **Quotes are fetched live.** `/api/stock/{secid}` costs ~1 s because it calls upstream, and returns `ts` (epoch) so freshness is explicit. Differences against a second read of upstream are tick movement, not staleness.
- **The newest bar can be live.** Responses carry `last_final` plus a `note`, so an agent can tell a settled bar from one still forming.
- **Delivery is plain JSON over HTTP**, gzip when asked (5x on a large payload), no auth and no cookies; pagination exists only on `/api/market/{id}`.
- **Known rough edges**: unknown ids return 200 with empty data, an unknown sub-resource falls through to the full snapshot, HEAD is unsupported, and the first request for an uncached symbol costs 1-5 s while it fetches upstream.

## 1. Endpoint inventory

| Route | Purpose | Status | Latency | Bytes (wire) | Encoding | Top-level keys |
| --- | --- | --- | --- | --- | --- | --- |
| `/api/health` | liveness | 200 | 4 ms | 32 | identity | ok, time |
| `/api/status` | system status | 200 | 1 ms | 2855 | gzip | client, clock, counts, events, intel, last_run, markets_live, markets_open |
| `/api/overview` | indices, groups, movers | 200 | 1 ms | 10424 | gzip | clock, collector, counts, generated_at, groups, movers, proxy, runs |
| `/api/markets` | market catalogue | 200 | 0 ms | 1088 | gzip | groups |
| `/api/market/cn.a` | one market's members | 200 | 11 ms | 1026 | gzip | error, market, order, page, rows, session, size, sort |
| `/api/movers` | top gainers/losers | 200 | 18 ms | 973 | gzip | active, gainers, losers |
| `/api/search` | symbol search | 200 | 8 ms | 169 | identity | query, results |
| `/api/watchlist` | watchlist | 200 | 1 ms | 13 | identity | items |
| `/api/spark` | sparkline series | 200 | 1 ms | 906 | identity | series |
| `/api/activity` | collector log | 200 | 1 ms | 1624 | gzip | events, runs |
| `/api/stock/1.600519` | single stock snapshot | 200 | 1061 ms | 1296 | gzip | amount, amplitude, avg_price, bid_diff, bid_ratio, bvps, change, code |
| `/api/stock/1.600519/kline` | daily bars | 200 | 18 ms | 2292 | gzip | as_of, bars, klt, last_date, last_final, live, note, secid |
| `/api/stock/1.600519/flow` | daily capital flow | 200 | 2669 ms | 776 | gzip | rows, secid |
| `/api/kline/1.600519` | bars (legacy alias) | 200 | 6 ms | 2291 | gzip | as_of, bars, klt, last_date, last_final, live, note, secid |
| `/api/signals/summary` | T+1 signal counts | 200 | 25 ms | 884 | gzip | day, hub, retention_days, sources, window |
| `/api/signals/limit_up` | limit-up pool | 200 | 2 ms | 1170 | gzip | day, rows, source |
| `/api/signals/stock/0.002131` | signals for one stock | 200 | 2 ms | 1102 | gzip | secid, signals |
| `/api/intel/sections` | news sections | 200 | 4 ms | 438 | gzip | hub, retention_days, sections, total |
| `/api/intel/articles` | article list | 200 | 1 ms | 622 | gzip | count, items, section |
| `/api/intel/digest` | agent digest | 200 | 1 ms | 603 | gzip | count, days, items |
| `/api/v1/meta` | protocol metadata | 200 | 6 ms | 763 | gzip | as_of_default, coverage, endpoints, glossary, name, rules, schema, units |
| `/api/v1/regime` | market regime packet | 200 | 15 ms | 2176 | gzip | as_of, caveats, facts, generated_at, kind, schema, sources, trade_date |
| `/api/v1/candidates` | ranked candidates | 200 | 49 ms | 1219 | gzip | as_of, candidates, caveats, count, generated_at, kind, method, schema |
| `/api/v1/stock/0.002131/evidence` | stock evidence | 200 | 2446 ms | 1441 | gzip | as_of, caveats, facts, generated_at, kind, name, schema, secid |
| `/api/v1/news/search` | news search | 200 | 9 ms | 533 | identity | as_of, caveats, count, generated_at, items, kind, query, schema |
| `/api/v1/brief` | markdown brief | 200 | 53 ms | 935 | gzip | — |
| `/api/v1/openapi.json` | OpenAPI spec | 200 | 3 ms | 1624 | gzip | components, info, openapi, paths, servers |
| `/api/v1/predictions/scorecard` | scorecard | 200 | 29 ms | 39 | identity | days, pending, scored |

## 2. K-line correctness vs upstream

Each row compares the bars this server serves against the same request made directly to EastMoney, field by field. A bar the server itself flags as `last_final: false` is still forming, so it is excluded from the match count and reported as lag instead.

| Instrument | Period | Served | Compared | Mismatched | Anomalies | Last bar final | Range |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 贵州茅台 A股 `1.600519` | 日K | 60 | 59 | 0 | 0 | False | 2026-08-07 → 2026-11-06 |
| 贵州茅台 A股 `1.600519` | 周K | 60 | 58 | 0 | 1 | False | 2025-09-19 → 2026-11-06 |
| 利欧股份 A股 `0.002131` | 日K | 60 | 59 | 0 | 0 | False | 2026-08-07 → 2026-11-06 |
| 利欧股份 A股 `0.002131` | 周K | 60 | 59 | 0 | 0 | False | 2025-09-12 → 2026-11-06 |
| 上证指数 `1.000001` | 日K | 60 | 59 | 0 | 0 | False | 2026-08-07 → 2026-11-06 |
| 上证指数 `1.000001` | 周K | 60 | 59 | 0 | 0 | False | 2025-09-12 → 2026-11-06 |
| 腾讯控股 港股 `116.00700` | 日K | 60 | 59 | 0 | 0 | False | 2026-08-13 → 2026-11-06 |
| 腾讯控股 港股 `116.00700` | 周K | 60 | 59 | 0 | 0 | False | 2025-09-19 → 2026-11-06 |
| Apple 美股 `105.AAPL` | 日K | 60 | 60 | 0 | 0 | True | 2026-08-13 → 2026-11-05 |
| Apple 美股 `105.AAPL` | 周K | 60 | 60 | 0 | 0 | True | 2025-09-19 → 2026-11-05 |

Forming bars (live session, excluded from the count):

- 贵州茅台 A股 `1.600519` 日K 2026-11-06: close 1446.96 vs 1448.08, volume 13148.0 vs 13283.0, amount 1906456279.0 vs 1925989108.0
- 贵州茅台 A股 `1.600519` 周K 2026-11-06: close 1448.63 vs 1448.63, volume 282244.0 vs 282244.0, amount 40890881064.0 vs 40890881064.0
- 利欧股份 A股 `0.002131` 日K 2026-11-06: close 4.41 vs 4.41, volume 3870854.0 vs 3873771.0, amount 1702661074.69 vs 1703947470.69
- 利欧股份 A股 `0.002131` 周K 2026-11-06: close 4.41 vs 4.41, volume 16330979.0 vs 16330979.0, amount 6739618120.71 vs 6739618120.71
- 上证指数 `1.000001` 日K 2026-11-06: close 3643.66 vs 3643.58, volume 171430578.0 vs 171539480.0, amount 280977407057.7 vs 281165240226.0
- 上证指数 `1.000001` 周K 2026-11-06: close 3643.58 vs 3643.58, volume 2444446722.0 vs 2444446722.0, amount 3872430525933.8 vs 3872430525933.8
- 腾讯控股 港股 `116.00700` 日K 2026-11-06: close 427.6 vs 427.6, volume 8928484.0 vs 8928984.0, amount 3780493312.0 vs 3780707120.0
- 腾讯控股 港股 `116.00700` 周K 2026-11-06: close 427.8 vs 428.0, volume 127124478.0 vs 127125078.0, amount 52224637472.0 vs 52224894224.0

Differences observed:

- `1.600519` 周K 2026-11-06: pct 4.06 != implied -0.23 (vendor field; closes match)

## 3. Quote correctness vs upstream

- **贵州茅台 A股** `1.600519` — match (458 ms, cached 1s ago)
- **利欧股份 A股** `0.002131` — match (950 ms, cached 2s ago)
- **上证指数** `1.000001` — tick movement (3 fields, each within 0.2%) (1023 ms, cached 4s ago)
  - `price`: served `3643.47` vs upstream `3643.76`
  - `volume`: served `171847680.0` vs upstream `171980550.0`
  - `amount`: served `281739333711.6` vs upstream `281969686252.5`
- **腾讯控股 港股** `116.00700` — tick movement (2 fields, each within 0.2%) (1017 ms, cached 2s ago)
  - `volume`: served `9155684.0` vs upstream `9156584.0`
  - `amount`: served `3877695952.0` vs upstream `3878080592.0`
- **Apple 美股** `105.AAPL` — match (1041 ms, cached 17766s ago)

## 4. Delivery semantics

- **Compression**: `Accept-Encoding: gzip` → `gzip` (10511 B vs 53183 B plain, 5.06×). Responses under 1 KB stay uncompressed (`identity`).
- **HEAD**: `{'error': "HTTP Error 501: Unsupported method ('HEAD')"}`.
- **Unknown market**: HTTP `404` — `{"error": "unknown market nope", "status": 404}`
- **Unknown secid**: HTTP `200`, 0 bars (empty rather than an error).
- **Limit clamp**: requested 9999, served 180.
- **Invalid klt**: HTTP `200`, echoes klt=`101`.
- **Typo in sub-resource** (`/bars`): HTTP `200` with keys `['code', 'depth', 'errors', 'flow', 'flow_reliable', 'name', 'partial', 'pre_close']`.
- **Pagination** on `/api/market/cn.a`: keys `['error', 'market', 'order', 'page', 'rows', 'session', 'size', 'sort', 'stats', 'total']`, 5 rows returned.
- **v1 error format**: HTTP `400` — `{"error": "invalid secid 'not-a-secid'; expected market.code like 1.600519", "status": 400}`

## 5. Issues found

- none

## 6. Field semantics an agent must know

| Field | Unit | Note |
| --- | --- | --- |
| `price`, `open`, `high`, `low`, `pre_close` | 元 | A-share; HK in HKD, US in USD |
| `pct` | percent | `1.0` means 1%, not 0.01 |
| `volume` | 手 | A-share lots (100 shares). HK/US use shares — check `market` |
| `amount` | 元 | turnover, already price × volume |
| `ts` | epoch seconds | when this snapshot was taken |
| `bars[].d` | `YYYY-MM-DD` | trading day, not a timestamp |
| `last_final` | bool | false means the last bar is still forming |
| `updated_at` | epoch seconds | collector write time on list endpoints |

Prices are already adjusted (`fqt=1`, 前复权) for the K-line endpoints; the snapshot's `close`/`price` is raw. Mixing the two across a split or dividend will produce a false gap.

## 7. How to consume it safely

1. Read `last_final` before using the newest bar. If it is false, treat that bar as provisional or drop it.
2. Use `ts` / `updated_at` rather than assuming freshness; a request outside the instrument's session returns the last close, not a live price.
3. Never infer an error from an empty `bars` array - check HTTP status *and* the `error`/`notes` keys, because unknown ids return 200.
4. On the v1 surface, pass `as_of` explicitly when replaying and rely on `caveats` for what was unavailable at that time.
5. Expect 1-5 s on the first request for an uncached symbol (upstream fetch), single-digit ms afterwards.
