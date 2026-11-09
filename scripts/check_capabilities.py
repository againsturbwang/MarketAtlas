"""Check the four capability questions against the running server."""
import ast, json, os, sys, time, gzip, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8787"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "audit", "Accept-Encoding": "gzip"}), timeout=40) as r:
            raw = r.read()
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return r.status, json.loads(raw), int((time.time() - t) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:160], int((time.time() - t) * 1000)
    except Exception as e:
        return 0, str(e)[:120], int((time.time() - t) * 1000)


print("### 1. all markets")
st, d, ms = get("/api/markets")
print(f"  status={st} markets={d.get('markets')} unique_instruments={d.get('unique_instruments')} {ms}ms")
print(f"  groups={len(d.get('groups') or [])}, every market carries count + session:",
      all("count" in m and "session" in m for g in d["groups"] for m in g["markets"]))

print("\n### 2. all stock codes")
st, d, ms = get("/api/instruments", {"size": 1000})
print(f"  /api/instruments size=1000 -> status={st} total={d.get('total')} pages={d.get('pages')} "
      f"rows={len(d.get('rows') or [])} {ms}ms")
st, d2, ms = get("/api/instruments", {"size": 1000, "page": 2})
print(f"  page 2 -> rows={len(d2.get('rows') or [])} first={d2['rows'][0]['secid']} "
      f"next={d2.get('next')}")
st, d3, _ = get("/api/instruments", {"market": "cn.a", "size": 5})
print(f"  filtered cn.a -> total={d3.get('total')}")
st, d4, _ = get("/api/instruments", {"kind": "index", "size": 5})
print(f"  filtered kind=index -> total={d4.get('total')}")
codes = {r["secid"] for r in (d.get("rows") or [])} | {r["secid"] for r in (d2.get("rows") or [])}
print(f"  distinct secids across two pages: {len(codes)} (no overlap expected)")

print("\n### 3. query by time")
cases = [
    ("as_of (v1 regime)", "/api/v1/regime", {"as_of": "2026-11-05T15:00"}),
    ("as_of (v1 candidates)", "/api/v1/candidates", {"as_of": "2026-11-05T18:30", "limit": 3}),
    ("day (signals)", "/api/signals/limit_up", {"day": "2026-11-05", "limit": 3}),
    ("days (news)", "/api/intel/articles", {"days": 1, "limit": 3}),
    ("range (kline)", "/api/stock/1.600519/kline",
     {"klt": 101, "from": "2026-09-01", "to": "2026-10-01", "limit": 100}),
]
for label, path, params in cases:
    st, d, ms = get(path, params)
    if "bars" in d:
        b = d["bars"]
        detail = f"{len(b)} bars {b[0]['d']}..{b[-1]['d']}" if b else "no bars"
    elif "candidates" in d:
        detail = f"{d['count']} candidates"
    elif "rows" in d:
        detail = f"{len(d['rows'])} rows"
    elif "items" in d:
        detail = f"{d['count']} items"
    else:
        detail = f"facts={len(d.get('facts') or [])}"
    print(f"  {label:<24} {path:<32} -> {st} {detail} ({ms}ms)")

print("\n### 4. query by stock code")
for path in ("/api/stock/1.600519", "/api/stock/1.600519/kline",
             "/api/stock/1.600519/flow", "/api/signals/stock/1.600519",
             "/api/v1/stock/1.600519/evidence", "/api/search?q=600519"):
    st, d, ms = get(path)
    size = len(json.dumps(d, ensure_ascii=False)) if not isinstance(d, str) else len(d)
    print(f"  {path:<38} -> {st} {size:>7}b {ms:>6}ms")

print("\n### 5. dependencies")
stdlib = sys.stdlib_module_names
local = {os.path.splitext(f)[0] for f in os.listdir(ROOT)
         if f.endswith(".py") and os.path.isfile(os.path.join(ROOT, f))}
third_party = set()
for name in sorted(local):
    try:
        tree = ast.parse(open(os.path.join(ROOT, name + ".py"), encoding="utf-8").read())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module.split(".")[0]]
        for m in mods:
            if m not in stdlib and m not in local:
                third_party.add(m)
print(f"  local modules: {len(local)}")
print(f"  non-stdlib imports: {sorted(third_party) or 'none'}")

hosts = set()
for name in sorted(local):
    text = open(os.path.join(ROOT, name + ".py"), encoding="utf-8").read()
    import re
    hosts |= set(re.findall(r"https?://([a-z0-9.-]+)", text))
print(f"  outbound hosts referenced: {sorted(h for h in hosts if h != '127.0.0.1')}")
print(f"  requirements.txt: {open(os.path.join(ROOT, 'requirements.txt')).read().strip()!r}")
