"""MCP server exposing the T+1 evidence protocol to any MCP host.

    python mcp_server.py                 # stdio transport
    python mcp_server.py --selftest      # print the tool manifest and exit

Point it at a running dashboard (default http://127.0.0.1:8787) with EM_BASE_URL.
Every tool is a thin wrapper over /api/v1/*, so an MCP client and a plain HTTP
client see exactly the same data.

Example client config (Claude Desktop / Codex style):

    {"mcpServers": {"eastmoney": {
        "command": "python",
        "args": ["A:\\\\Scraper\\\\EastMoney\\\\mcp_server.py"],
        "env": {"EM_BASE_URL": "http://127.0.0.1:8787"}}}}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("EM_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
PROTOCOL = "2024-11-05"

AS_OF_DOC = ("Knowledge cutoff, YYYY-MM-DD or ISO datetime (Asia/Shanghai). "
             "Defaults to now, anchored to the last session close. Nothing published "
             "after this instant is included, which makes past dates replayable.")


def call_http(path: str, params: dict | None = None, body: dict | None = None,
              timeout: float = 30.0) -> dict:
    url = BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/markdown"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if "json" in (resp.headers.get("Content-Type") or ""):
            return json.loads(raw.decode("utf-8"))
        return {"text": raw.decode("utf-8", "replace")}


TOOLS = [
    {
        "name": "market_regime",
        "description": (
            "Market-wide context for one session: index moves, A-share breadth, the "
            "limit-up/limit-down pools with 连板 heights, sector fund flows, 沪深港通 "
            "flows, margin totals and news volume. Start here when judging the overall "
            "market before picking names."
        ),
        "inputSchema": {"type": "object", "properties": {
            "as_of": {"type": "string", "description": AS_OF_DOC}}},
    },
    {
        "name": "screen_candidates",
        "description": (
            "Stocks ranked by signal confluence: limit-up state, 龙虎榜 net buy, main "
            "fund flow ratio, margin net buy, popularity rank change, research coverage. "
            "Each candidate carries `reasons` with the signal, value and weight, so you "
            "can judge the ranking instead of trusting it. This is attention ranking, "
            "not a return forecast."
        ),
        "inputSchema": {"type": "object", "properties": {
            "as_of": {"type": "string", "description": AS_OF_DOC},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}}},
    },
    {
        "name": "stock_evidence",
        "description": (
            "Everything known about one instrument at the cutoff: price and momentum "
            "(1/5/20 day), every T+1 signal attached to it, and related news headlines. "
            "Use this before forming a view on a specific name."
        ),
        "inputSchema": {"type": "object", "required": ["secid"], "properties": {
            "secid": {"type": "string", "description": "market.code, e.g. 1.600519 or 0.002131"},
            "as_of": {"type": "string", "description": AS_OF_DOC}}},
    },
    {
        "name": "search_news",
        "description": (
            "Full-text search over the seven-day article store (财经导读, 7x24快讯, "
            "东方视点, 全球, 焦点专题, 社区), cut at the cutoff. Returns excerpts and "
            "art_ids you can pass to read_article."
        ),
        "inputSchema": {"type": "object", "properties": {
            "q": {"type": "string", "description": "keyword, e.g. 白酒 or 并购"},
            "as_of": {"type": "string", "description": AS_OF_DOC},
            "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 7},
            "limit": {"type": "integer", "default": 15, "minimum": 1, "maximum": 100}}},
    },
    {
        "name": "read_article",
        "description": "Full extracted body of one article or announcement, by art_id.",
        "inputSchema": {"type": "object", "required": ["art_id"], "properties": {
            "art_id": {"type": "string"}}},
    },
    {
        "name": "list_signals",
        "description": (
            "Raw T+1 signal table when you want to look yourself rather than take a "
            "synthesis: limit_up, limit_down, dragon_tiger, stock_flow, sector_flow, "
            "margin, connect, popularity, research, announcement."
        ),
        "inputSchema": {"type": "object", "required": ["source"], "properties": {
            "source": {"type": "string", "enum": [
                "limit_up", "limit_down", "dragon_tiger", "stock_flow", "sector_flow",
                "margin", "connect", "popularity", "research", "announcement"]},
            "day": {"type": "string", "description": "YYYY-MM-DD, defaults to the latest day"},
            "limit": {"type": "integer", "default": 50, "minimum": 5, "maximum": 300}}},
    },
    {
        "name": "report_prediction",
        "description": (
            "Record your call so it can be scored. The platform compares it against the "
            "next daily bar and feeds the scorecard. Always include a rationale and the "
            "evidence ids you relied on - unsupported calls are indistinguishable from "
            "lucky ones in the record."
        ),
        "inputSchema": {"type": "object", "required": ["secid", "direction"], "properties": {
            "secid": {"type": "string"},
            "direction": {"type": "string", "enum": ["up", "down", "flat"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "expected_return_pct": {"type": "number"},
            "as_of": {"type": "string", "description": AS_OF_DOC},
            "rationale": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "model": {"type": "string"}}},
    },
    {
        "name": "scorecard",
        "description": ("Hit rate, Brier score, per-direction performance and confidence "
                        "calibration of every prediction recorded so far."),
        "inputSchema": {"type": "object", "properties": {
            "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365}}},
    },
]


def run_tool(name: str, args: dict) -> dict:
    if name == "market_regime":
        return call_http("/api/v1/regime", {"as_of": args.get("as_of")})
    if name == "screen_candidates":
        return call_http("/api/v1/candidates", {"as_of": args.get("as_of"),
                                                "limit": args.get("limit", 20)})
    if name == "stock_evidence":
        secid = urllib.parse.quote(str(args["secid"]), safe="")
        return call_http(f"/api/v1/stock/{secid}/evidence", {"as_of": args.get("as_of")})
    if name == "search_news":
        return call_http("/api/v1/news/search", {
            "q": args.get("q"), "as_of": args.get("as_of"),
            "days": args.get("days", 7), "limit": args.get("limit", 15)})
    if name == "read_article":
        art_id = urllib.parse.quote(str(args["art_id"]), safe="")
        return call_http(f"/api/v1/article/{art_id}")
    if name == "list_signals":
        return call_http(f"/api/signals/{urllib.parse.quote(str(args['source']), safe='')}", {
            "day": args.get("day"), "limit": args.get("limit", 50)})
    if name == "report_prediction":
        body = {k: args[k] for k in ("secid", "direction", "confidence",
                                     "expected_return_pct", "as_of", "rationale",
                                     "evidence", "model") if args.get(k) is not None}
        return call_http("/api/v1/predictions", body=body)
    if name == "scorecard":
        return call_http("/api/v1/predictions/scorecard", {"days": args.get("days", 30)})
    raise ValueError(f"unknown tool {name}")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(msg_id, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def error(msg_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def handle(message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        reply(msg_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "eastmoney-t1", "version": "1.0.0"},
        })
    elif method in ("notifications/initialized", "initialized"):
        return
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            payload = run_tool(name, args)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            reply(msg_id, {"content": [{"type": "text", "text": f"HTTP {exc.code}: {detail}"}],
                           "isError": True})
            return
        except Exception as exc:                     # noqa: BLE001 - reported to the client
            reply(msg_id, {"content": [{"type": "text",
                                        "text": f"{type(exc).__name__}: {exc}"}],
                           "isError": True})
            return
        text = payload.pop("text", None) or json.dumps(payload, ensure_ascii=False,
                                                       separators=(",", ":"))
        reply(msg_id, {"content": [{"type": "text", "text": text}]})
    elif method in ("resources/list", "prompts/list"):
        reply(msg_id, {"resources": [], "prompts": []} if method == "resources/list"
              else {"prompts": []})
    else:
        error(msg_id, -32601, f"method not found: {method}")


def main() -> int:
    if "--selftest" in sys.argv:
        print(json.dumps({"base": BASE, "tools": [t["name"] for t in TOOLS]},
                         ensure_ascii=False, indent=2))
        try:
            regime = call_http("/api/v1/regime")
            print("live check: ok, as_of", regime.get("as_of"),
                  "facts", len(regime.get("facts", [])))
        except Exception as exc:                     # noqa: BLE001
            print("live check failed:", exc)
        return 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            handle(message)
        except Exception as exc:                     # noqa: BLE001 - never kill the loop
            if message.get("id") is not None:
                error(message["id"], -32603, f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
