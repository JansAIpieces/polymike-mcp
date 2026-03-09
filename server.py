"""PolyMike MCP Server — Polymarket intelligence & trading for AI agents.

Fully standalone — no local imports or file paths.
Direct calls to Polymarket Gamma + CLOB APIs only.

═══════════════════════════════════════════════════════════════
                    HYBRID PRICING MODEL
═══════════════════════════════════════════════════════════════

Light tools (included in subscription):
  - search_markets       Search markets by keyword
  - resolve_market       Resolve URL or condition_id
  - get_orderbook        Live orderbook with bid/ask
  - get_market_info      Full market data + midpoints
  - get_market_history   Price history for a token

Heavy tools (per-call billing):
  - snipe_market         Execute a buy order ($0.10/call)

═══════════════════════════════════════════════════════════════
                MCPize PRICING TAB SETUP
═══════════════════════════════════════════════════════════════

In the MCPize Developer Dashboard -> your server -> Pricing tab:

1. BASE SUBSCRIPTION (Monthly Recurring):
   - Free tier:  $0/month  — 10 calls/day across all tools
   - Basic tier: $9/month  — unlimited light tools
   - Pro tier:   $19/month — unlimited light tools + access to heavy tools

2. USAGE-BASED (Per-Call, on top of subscription):
   - Light tools: $0.00 per call (included in any subscription)
   - snipe_market: $0.10 per call (Pro tier required)

3. TOOL ACCESS:
   - In the "Tool Access" section, restrict snipe_market to Pro tier only
   - All other tools available to all tiers

═══════════════════════════════════════════════════════════════

Deploy: Push to GitHub -> connect in MCPize dashboard
"""

import re
import json
import time
import httpx
from fastmcp import FastMCP

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# War Room bot local API (for authenticated trading)
WAR_ROOM_API = "http://localhost:8200"

# Rate limiter for snipe_market (5 calls/minute global)
_snipe_calls: list[float] = []
SNIPE_RATE_LIMIT = 5
SNIPE_RATE_WINDOW = 60  # seconds

mcp = FastMCP(
    "polymike-intelligence",
    description=(
        "Polymarket intelligence & trading tools for AI agents. "
        "Search markets, get live orderbooks, resolve URLs, and execute trades. "
        "Light tools (search, orderbook, resolve) included in subscription. "
        "Heavy tools (snipe_market) billed per call at $0.10, Pro tier required."
    ),
)


# ── Helpers ──────────────────────────────────────────────────

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


def _check_snipe_rate_limit() -> bool:
    """Return True if under rate limit, False if exceeded."""
    now = time.time()
    _snipe_calls[:] = [t for t in _snipe_calls if now - t < SNIPE_RATE_WINDOW]
    if len(_snipe_calls) >= SNIPE_RATE_LIMIT:
        return False
    _snipe_calls.append(now)
    return True


# ── Light Tools (included in subscription) ───────────────────

@mcp.tool()
async def search_markets(query: str, limit: int = 5) -> list[dict]:
    """Search active Polymarket markets by keyword.

    Returns matching markets with question, condition_id, tokens,
    volume, liquidity, and end date. Sorted by volume descending.
    """
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
    """Resolve a Polymarket URL or condition_id to full market data.

    Accepts:
    - A condition_id (0x...)
    - A polymarket.com event URL

    For multi-market events, returns a list of all sub-markets.
    """
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
    """Get live orderbook for a Polymarket token.

    Returns best bid/ask, midpoint, spread, and top 5 levels
    on each side. Prices are in 0-1 range (multiply by 100 for cents).
    """
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
            "bid_depth": len(bids),
            "ask_depth": len(asks),
            "top_bids": [{"price": float(b["price"]), "size": float(b["size"])} for b in bids[-5:]],
            "top_asks": [{"price": float(a["price"]), "size": float(a["size"])} for a in asks[-5:]],
        }


@mcp.tool()
async def get_market_info(condition_id: str) -> dict:
    """Get detailed market info with live midpoint prices for each outcome.

    Combines Gamma market data with live CLOB midpoints.
    Use this for a complete market overview before trading.
    """
    data = await _gamma_get("/markets", params={"condition_id": condition_id})
    if not data or not isinstance(data, list) or not data:
        return {"error": "Market not found"}

    market = _normalize_market(data[0])

    async with httpx.AsyncClient(timeout=10) as client:
        for token in market.get("tokens", []):
            tid = token.get("token_id", "")
            if tid:
                try:
                    resp = await client.get(
                        f"{CLOB_API}/midpoint", params={"token_id": tid}
                    )
                    if resp.status_code == 200:
                        token["midpoint"] = float(resp.json().get("mid", 0))
                except Exception:
                    pass
    return market


@mcp.tool()
async def get_market_history(token_id: str, interval: str = "1d", fidelity: int = 60) -> dict:
    """Get price history for a Polymarket token.

    Args:
        token_id: The CLOB token ID
        interval: Time range — "1d", "1w", "1m", "all"
        fidelity: Minutes between data points (default 60)

    Returns time series of midpoint prices.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CLOB_API}/prices-history",
            params={
                "market": token_id,
                "interval": interval,
                "fidelity": fidelity,
            },
        )
        if resp.status_code != 200:
            return {"error": "Failed to fetch price history"}

        history = resp.json().get("history", [])
        if not history:
            return {"error": "No price history available"}

        prices = [{"time": h.get("t", 0), "price": float(h.get("p", 0))} for h in history]
        return {
            "token_id": token_id,
            "interval": interval,
            "data_points": len(prices),
            "latest_price": prices[-1]["price"] if prices else 0,
            "prices": prices,
        }


# ── Heavy Tools (per-call billing, Pro tier only) ────────────

@mcp.tool()
async def snipe_market(
    condition_id: str,
    side: str,
    size_usd: float,
    api_key: str,
) -> dict:
    """Execute a buy order on Polymarket via the PolyMike trading API.

    Pro tier only — $0.10 per call.

    NOTE: This tool requires the PolyMike War Room bot running locally
    on your machine. It delegates to localhost:8200. Private keys
    NEVER leave your local environment. On MCPize cloud, this tool
    will return an error — use local MCP mode for trading.

    Args:
        condition_id: Market condition_id (0x...)
        side: "YES" or "NO"
        size_usd: Dollar amount to spend
        api_key: Your PolyMike API key (from /start in the Telegram bot)

    Returns order status, order_id, tokens bought, and amount spent.
    """
    side = side.upper()
    if side not in ("YES", "NO"):
        return {"error": "side must be 'YES' or 'NO'"}
    if size_usd <= 0 or size_usd > 1000:
        return {"error": "size_usd must be between $0.01 and $1000"}
    if not condition_id.startswith("0x"):
        return {"error": "condition_id must start with 0x"}

    # Rate limit: 5 calls/minute
    if not _check_snipe_rate_limit():
        return {"error": "Rate limited — max 5 snipe calls per minute. Wait and retry."}

    # Delegate to local War Room trading API
    # Private keys stay on the user's machine — we only send the API key
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{WAR_ROOM_API}/api/snipe",
                json={
                    "api_key": api_key,
                    "condition_id": condition_id,
                    "side": side,
                    "size_usd": size_usd,
                },
            )
            if resp.status_code == 200:
                return resp.json()
            return {
                "error": f"Trading API returned {resp.status_code}",
            }
    except httpx.ConnectError:
        return {
            "error": "Cannot reach PolyMike trading API",
            "hint": "Make sure the War Room bot is running locally (python -B -m war_room.bot)",
        }
    except Exception as e:
        return {"error": f"Request failed: {str(e)[:100]}"}


if __name__ == "__main__":
    mcp.run()
