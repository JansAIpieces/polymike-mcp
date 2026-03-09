"""PolyMike MCP Server — Polymarket intelligence tools for AI agents.

Fully standalone — no local dependencies. Calls Polymarket APIs directly.

Tools:
  search_markets  — Search Polymarket by keyword
  resolve_market  — Resolve a Polymarket URL to market data
  get_orderbook   — Get current bid/ask for a market token
  get_market_info — Get detailed info about a specific market

Run locally:  python server.py
Deploy:       Push to GitHub -> connect via MCPize dashboard
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
    "Search markets, resolve URLs, get orderbooks and market data.",
)


async def _gamma_get(path: str, params: dict | None = None) -> dict | list | None:
    """Helper for Gamma API calls."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GAMMA_API}{path}", params=params or {})
        if resp.status_code == 200:
            return resp.json()
    return None


def _normalize_market(m: dict) -> dict:
    """Normalize Gamma market data into clean format."""
    # Parse outcomes and token IDs from JSON strings
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

    # Build tokens array
    tokens = []
    for i, outcome in enumerate(outcomes_list):
        token_id = token_ids_list[i] if i < len(token_ids_list) else ""
        tokens.append({"outcome": outcome, "token_id": token_id})

    # Also check existing tokens array
    if not tokens and m.get("tokens"):
        tokens = m["tokens"]

    condition_id = m.get("condition_id", m.get("conditionId", ""))
    return {
        "question": m.get("question", ""),
        "condition_id": condition_id,
        "tokens": tokens,
        "volume": m.get("volume", 0),
        "liquidity": m.get("liquidity", 0),
        "end_date": m.get("end_date_iso", ""),
        "active": m.get("active", True),
        "closed": m.get("closed", False),
    }


@mcp.tool()
async def search_markets(query: str, limit: int = 5) -> list[dict]:
    """Search Polymarket for markets matching a keyword.

    Args:
        query: Search keywords (e.g. 'iran strike', 'trump tariff', 'bitcoin')
        limit: Max results (1-20, default 5)

    Returns list of markets with question, condition_id, outcomes, volume.
    """
    limit = min(max(limit, 1), 20)

    # Gamma API has no text search — fetch top markets by volume, filter locally
    data = await _gamma_get("/markets", params={
        "limit": 200,
        "active": "true",
        "closed": "false",
        "order": "volume",
        "ascending": "false",
    })
    if not data:
        return [{"error": "Failed to fetch markets from Polymarket API"}]

    query_lower = query.lower()
    keywords = query_lower.split()
    matches = []
    for m in data:
        q = m.get("question", "").lower()
        if all(kw in q for kw in keywords):
            matches.append(_normalize_market(m))
            if len(matches) >= limit:
                break

    if not matches:
        return [{"info": f"No markets found matching '{query}'. Try broader keywords or use resolve_market with a URL."}]
    return matches


@mcp.tool()
async def resolve_market(url: str) -> dict | list[dict]:
    """Resolve a Polymarket URL to full market data.

    Args:
        url: Polymarket URL (e.g. https://polymarket.com/event/iran-deal/will-iran...)
            or a condition_id starting with 0x

    Returns market data with question, outcomes, condition_id.
    For parent event URLs with multiple sub-markets, returns a list.
    """
    # Handle condition_id directly
    if url.startswith("0x"):
        data = await _gamma_get("/markets", params={"condition_id": url})
        if data and isinstance(data, list) and data:
            return _normalize_market(data[0])
        return {"error": f"Market not found for condition_id {url[:20]}..."}

    # Extract slug from URL
    # Formats: /event/slug/market-slug or /event/slug
    match = re.search(r"polymarket\.com/event/([^/?#]+)(?:/([^/?#]+))?", url)
    if not match:
        return {"error": "Could not parse Polymarket URL. Expected format: https://polymarket.com/event/..."}

    event_slug = match.group(1)
    market_slug = match.group(2)

    # Fetch event
    data = await _gamma_get("/events", params={"slug": event_slug})
    if not data or not isinstance(data, list) or not data:
        return {"error": f"Event not found: {event_slug}"}

    event = data[0]
    markets = event.get("markets", [])

    if not markets:
        return {"error": "Event has no markets"}

    # If specific market slug, find it
    if market_slug:
        for m in markets:
            slug = m.get("market_slug", m.get("slug", ""))
            if market_slug in slug or slug in market_slug:
                return _normalize_market(m)

    # Multiple markets -> return list
    if len(markets) > 1:
        return [_normalize_market(m) for m in markets]

    return _normalize_market(markets[0])


@mcp.tool()
async def get_orderbook(token_id: str) -> dict:
    """Get the current orderbook for a market token.

    Args:
        token_id: The CLOB token ID (get this from search_markets or resolve_market)

    Returns best bid/ask, midpoint, spread, and top order levels.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        if resp.status_code != 200:
            return {"error": f"Failed to fetch orderbook (HTTP {resp.status_code})"}

        book = resp.json()

    # CLOB sort: bids ascending, asks descending
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
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "top_bids": [{"price": float(b["price"]), "size": float(b["size"])} for b in bids[-5:]],
        "top_asks": [{"price": float(a["price"]), "size": float(a["size"])} for a in asks[-5:]],
    }


@mcp.tool()
async def get_market_info(condition_id: str) -> dict:
    """Get detailed information about a specific market.

    Args:
        condition_id: The market condition ID (starts with 0x)

    Returns market question, outcomes with current prices, volume, liquidity, end date.
    """
    data = await _gamma_get("/markets", params={"condition_id": condition_id})
    if not data or not isinstance(data, list) or not data:
        return {"error": f"Market not found: {condition_id[:20]}..."}

    market = _normalize_market(data[0])

    # Fetch midpoints for each token
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
