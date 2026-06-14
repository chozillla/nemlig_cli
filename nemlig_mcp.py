#!/usr/bin/env python3
"""MCP server exposing nemlig.com operations as tools for Claude Code.

Wraps the domain functions in nemlig_cli.py and serves them over stdio
JSON-RPC. Register in ~/.claude.json under mcpServers.nemlig:

    {
      "mcpServers": {
        "nemlig": {
          "command": "uv",
          "args": ["--directory", "/Users/chemay/Documents/GitHub/Nemlig/nemlig_cli",
                   "run", "python", "nemlig_mcp.py"]
        }
      }
    }

Credentials load from .env (NEMLIG_USER / NEMLIG_PASS) or ~/.config/nemlig/login.json.
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import nemlig_cli as ncl

# Load .env from the script's directory so credentials are picked up when
# Claude Code launches this server with a clean environment.
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


mcp = FastMCP("nemlig")

_auth: "ncl.AuthTokens | None" = None


def _get_auth() -> "ncl.AuthTokens":
    """Lazy-login and cache tokens for the process lifetime."""
    global _auth
    if _auth is not None:
        return _auth
    creds = ncl.load_config_credentials()
    username = os.environ.get("NEMLIG_USER") or creds.get("username")
    password = os.environ.get("NEMLIG_PASS") or creds.get("password")
    if not username or not password:
        raise RuntimeError(
            "Missing nemlig credentials — set NEMLIG_USER / NEMLIG_PASS in env "
            "or username/password in ~/.config/nemlig/login.json"
        )
    _auth = ncl.login(username, password)
    return _auth


def _product_summary(p: dict) -> dict:
    """Trim a search-result product to the fields the model needs."""
    return {
        "id": p.get("Id") or p.get("ProductId"),
        "name": p.get("Name"),
        "price_kr": p.get("Price"),
        "brand": p.get("BrandName"),
        "package_size": p.get("PackageSizeText") or p.get("UnitText"),
        "url": p.get("Url"),
    }


@mcp.tool()
def search(query: str, limit: int = 10) -> list[dict]:
    """Search nemlig.com products. Query in Danish. Returns id, name, price_kr, brand, package_size."""
    products = ncl.search_products(_get_auth(), query, limit=limit)
    return [_product_summary(p) for p in products]


@mcp.tool()
def details(product_id: str) -> dict:
    """Get full product details by id, including per-100g nutrition when available."""
    product = ncl.get_product_details(_get_auth(), product_id)
    return {
        "id": product.get("Id"),
        "name": product.get("Name"),
        "price_kr": product.get("Price"),
        "brand": product.get("BrandName"),
        "description": product.get("Description"),
        "nutrition_per_100g": ncl.extract_nutrition(product),
    }


@mcp.tool()
def macros(query: str, limit: int = 5) -> list[dict]:
    """Search products and return per-100g macros + protein-per-krone, ranked descending.

    Use this when picking the most macro-efficient protein source for a meal plan.
    """
    auth = _get_auth()
    products = ncl.search_products(auth, query, limit=limit)
    out: list[dict] = []
    for p in products:
        pid = p.get("Id") or p.get("ProductId")
        if not pid:
            continue
        try:
            detail = ncl.get_product_details(auth, str(pid))
            nut = ncl.extract_nutrition(detail) or {}
        except Exception:
            nut = {}
        price = p.get("Price") or 0
        protein = nut.get("protein_g")
        out.append({
            "id": pid,
            "name": p.get("Name"),
            "price_kr": price,
            "nutrition_per_100g": nut,
            "protein_per_krone": (protein / price) if (protein and price) else None,
        })
    out.sort(key=lambda x: x.get("protein_per_krone") or 0, reverse=True)
    return out


@mcp.tool()
def recipes(query: str, count: int = 5) -> list[dict]:
    """Search nemlig.com recipes by keyword."""
    rs = ncl.search_recipes(_get_auth(), query, count=count)
    return [
        {
            "name": r.get("Name"),
            "url": r.get("Url"),
            "description": r.get("Description"),
            "image": r.get("Image"),
        }
        for r in rs
    ]


@mcp.tool()
def basket() -> dict:
    """Return the user's current nemlig.com basket."""
    return ncl.get_basket(_get_auth())


@mcp.tool()
def basket_add(product_id: str, quantity: int = 1) -> dict:
    """Add a product to the user's nemlig.com basket. Confirm with the user first."""
    return ncl.add_to_basket(_get_auth(), product_id, quantity=quantity)


@mcp.tool()
def history(order_id: int | None = None, take: int = 10) -> dict:
    """Order history. Pass order_id for one order's details, otherwise returns the most recent `take`."""
    auth = _get_auth()
    if order_id is not None:
        return ncl.get_order_details(auth, order_id)
    return ncl.get_order_history(auth, take=take)


# ---------------------------------------------------------------------------
# Local grocery list (file-backed at ~/.config/nemlig/grocery_list.json)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_show() -> dict:
    """Show the local grocery list (budget + items). No nemlig.com call."""
    return ncl.load_grocery_list()


@mcp.tool()
def list_add(query_or_id: str, quantity: int = 1) -> dict:
    """Add an item to the local grocery list.

    If query_or_id is a numeric product id, the product is added directly.
    Otherwise nemlig.com is searched and the top match is added.
    """
    auth = _get_auth()
    data = ncl.load_grocery_list()
    if query_or_id.isdigit():
        product = ncl.get_product_details(auth, query_or_id)
    else:
        results = ncl.search_products(auth, query_or_id, limit=1)
        if not results:
            return {"error": f"No products found for '{query_or_id}'"}
        product = ncl.get_product_details(auth, str(results[0].get("Id")))
    item = {
        "id": str(product.get("Id")),
        "name": product.get("Name"),
        "price": product.get("Price", 0),
        "quantity": quantity,
    }
    data["items"].append(item)
    ncl.save_grocery_list(data)
    return {"added": item, "total_items": len(data["items"])}


@mcp.tool()
def list_remove(product_id: str) -> dict:
    """Remove a product from the local grocery list by id."""
    data = ncl.load_grocery_list()
    before = len(data["items"])
    data["items"] = [x for x in data["items"] if str(x.get("id")) != str(product_id)]
    ncl.save_grocery_list(data)
    return {"removed": before - len(data["items"]), "remaining": len(data["items"])}


@mcp.tool()
def list_clear() -> dict:
    """Clear every item from the local grocery list. Destructive — confirm first."""
    data = ncl.load_grocery_list()
    count = len(data["items"])
    data["items"] = []
    ncl.save_grocery_list(data)
    return {"cleared": count}


@mcp.tool()
def list_budget(amount: float | None = None) -> dict:
    """Get the local grocery list budget, or set it if `amount` is provided."""
    data = ncl.load_grocery_list()
    if amount is not None:
        data["budget"] = float(amount)
        ncl.save_grocery_list(data)
    return {"budget_kr": data.get("budget"), "items": len(data["items"])}


@mcp.tool()
def list_sync() -> dict:
    """Push every item on the local grocery list to the nemlig.com basket. Confirm with the user first."""
    auth = _get_auth()
    data = ncl.load_grocery_list()
    ok, fail = [], []
    for item in data["items"]:
        try:
            ncl.add_to_basket(auth, str(item["id"]), quantity=item.get("quantity", 1))
            ok.append({"id": item["id"], "name": item["name"]})
        except Exception as e:
            fail.append({"id": item["id"], "name": item["name"], "error": str(e)})
    return {"synced": len(ok), "failed": len(fail), "results": {"ok": ok, "fail": fail}}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
