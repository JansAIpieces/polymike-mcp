"""PolyMike MCP Server — Standalone Polymarket intelligence for AI agents.

Fully independent — no local imports or file paths.
Direct calls to Polymarket Gamma + CLOB APIs only.

Tools:
- search_markets
- resolve_market
- get_orderbook
- get_market_info

Deploy: Push to GitHub -> connect in MCPize dashboard
"""

import re
import json
import httpx
from fastmcp import FastMCP

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

mcp = FastMCP(
    "polymike-intelligence",
    description="Polymarket geopolitical intelligence & trading tools. "
                "Search markets, resolve URLs, get orderbooks and live data.",
)


async def _gamma_get(path: str, params: dict | None = None) -> dict | list | None:
    """Helper for Gamma API calls."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GAMMA_API}{path}", params=params or {})
        if resp.status_code == 200:
            return resp.json()
        return None


def _normalize_market(m: dict) -> dict:
    """Clean Gamma market data for MCP response."""
    outcomes_raw = m.get("outcomes", "[]")
    token_ids_raw = m.get("clobTokenIds", "[]")

    if isinstance(outcomes_raw, str):
        try:
            outcomes_list = json.loads(outcomes_raw)
        except (json.JSONDecodeError, TypeError):
            outcomes_list = []
    else:
        outcomes_list = outcomes_raw or []

    if isinstance(token_ids_raw, str):
        try:
            token_ids_list = json.loads(token_ids_raw)
        except (json.JSONDecodeError, TypeError):
            token_ids_list = []
    else:
        token_ids_list = token_ids_raw or []

    tokens = []
    for i, outcome in enumerate(outcomes_list):
        token_id = token_ids_list[i] if i < len(token_ids_list) else ""
        tokens.append({"outcome": outcome, "token_id": token_id})

    if not tokens and m.get("tokens"):
        tokens = m["tokens"]

    return {
        "question": m.get("question", ""),
        "condition_id": m.get("condition_id", m.get("conditionId", "")),
        "tokens": tokens,
        "volume": m.get("volume", 0),
        "liquidity": m.get("liquidity", 0),
        "end_date": m.get("end_date_iso", ""),
        "active": m.get("active", True),
    }


@mcp.tool()
async def search_markets(query: str, limit: int = 5) -> list[dict]:
    """Search Polymarket markets by keyword."""
    limit = min(max(limit, 1), 20)
    data = await _gamma_get("/markets", params={
        "limit": 200,
        "active": "true",
        "closed": "false",
        "order": "volume",
        "ascending": "false",
    })
    if not data:
        return [{"error": "Failed to fetch markets"}]

    query_lower = query.lower()
    keywords = query_lower.split()
    matches = []
    for m in data:
        q = m.get("question", "").lower()
        if all(kw in q for kw in keywords):
            matches.append(_normalize_market(m))
            if len(matches) >= limit:
                break
    return matches or [{"info": f"No matches for '{query}'. Try broader terms."}]


@mcp.tool()
async def resolve_market(url_or_id: str) -> dict | list[dict]:
    """Resolve a Polymarket URL or condition_id to full market data."""
    if url_or_id.startswith("0x"):
        data = await _gamma_get("/markets", params={"condition_id": url_or_id})
        if data and isinstance(data, list) and data:
            return _normalize_market(data[0])
        return {"error": "Market not found"}

    match = re.search(r"polymarket\.com/event/([^/?#]+)", url_or_id)
    if not match:
        return {"error": "Invalid Polymarket URL"}

    event_slug = match.group(1)
    data = await _gamma_get("/events", params={"slug": event_slug})
    if not data or not isinstance(data, list) or not data:
        return {"error": "Event not found"}

    markets = data[0].get("markets", [])
    if len(markets) == 1:
        return _normalize_market(markets[0])
    return [_normalize_market(m) for m in markets]


@mcp.tool()
async def get_orderbook(token_id: str) -> dict:
    """Get current orderbook for a token."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        if resp.status_code != 200:
            return {"error": "Failed to fetch orderbook"}

        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[-1]["price"]) if bids else 0
        best_ask = float(asks[-1]["price"]) if asks else 0
        mid = round((best_bid + best_ask) / 2, 4) if best_bid and best_ask else 0
        spread = round(best_ask - best_bid, 4) if best_bid and best_ask else 0

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": mid,
            "spread": spread,
            "top_bids": [{"price": float(b["price"]), "size": float(b["size"])} for b in bids[-5:]],
            "top_asks": [{"price": float(a["price"]), "size": float(a["size"])} for a in asks[-5:]],
        }


@mcp.tool()
async def get_market_info(condition_id: str) -> dict:
    """Get detailed market info with live prices for each outcome."""
    data = await _gamma_get("/markets", params={"condition_id": condition_id})
    if not data or not isinstance(data, list) or not data:
        return {"error": "Market not found"}

    market = _normalize_market(data[0])

    async with httpx.AsyncClient(timeout=10) as client:
        for token in market.get("tokens", []):
            tid = token.get("token_id", "")
            if tid:
                try:
                    resp = await client.get(f"{CLOB_API}/midpoint", params={"token_id": tid})
                    if resp.status_code == 200:
                        token["midpoint"] = float(resp.json().get("mid", 0))
                except Exception:
                    pass
    return market


if __name__ == "__main__":
    mcp.run()
