#!/usr/bin/env python3
"""
Nemlig.com CLI - A command-line interface for nemlig.com grocery shopping.

Usage:
    python nemlig_cli.py search "cocio"
    python nemlig_cli.py details PRODUCT_ID
    python nemlig_cli.py basket
    python nemlig_cli.py add PRODUCT_ID [--quantity N]
    python nemlig_cli.py history [ORDER_ID]

Credentials can be provided via ~/.config/nemlig/login.json or CLI options.
CLI options override the config file.
"""

import argparse
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

# Conditional import for camera support
CAMERA_AVAILABLE = False
try:
    from picamera2 import Picamera2
    from picamera2.devices.imx500 import IMX500

    CAMERA_AVAILABLE = True
except ImportError:
    pass


CONFIG_FILE = Path.home() / ".config" / "nemlig" / "login.json"
INVENTORY_FILE = Path.home() / ".config" / "nemlig" / "inventory.txt"
SHOPPING_LIST_FILE = Path.home() / ".config" / "nemlig" / "shopping_list.txt"
DEFAULT_MODEL = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"


@dataclass
class InventoryItem:
    """A produce item in the home inventory."""

    name: str
    quantity: int
    last_seen: str  # ISO timestamp


@dataclass
class ShoppingItem:
    """An item on the shopping list."""

    name: str
    quantity: int
    added_date: str  # ISO timestamp


@dataclass
class Detection:
    """A single object detection result."""

    label: str
    confidence: float
    box: tuple  # (x, y, width, height)


# COCO class IDs for produce (YOLOv8 COCO classes)
COCO_PRODUCE_CLASSES = {
    46: "banana",
    47: "apple",
    49: "orange",
    50: "broccoli",
    51: "carrot",
}

# All produce we support (including manual-only items)
PRODUCE_CLASSES = {"banana", "apple", "orange", "broccoli", "carrot"}

# Mapping from produce names to Danish search terms for nemlig.com
PRODUCE_TO_NEMLIG = {
    "apple": "æble",
    "banana": "banan",
    "orange": "appelsin",
    "broccoli": "broccoli",
    "carrot": "gulerod",
    # Manual additions (camera can't detect, but shopping list supports)
    "tomato": "tomat",
    "cucumber": "agurk",
    "pepper": "peberfrugt",
    "lemon": "citron",
    "potato": "kartoffel",
    "onion": "løg",
}

# Restock thresholds - suggest for shopping list when below these quantities
RESTOCK_THRESHOLDS = {
    "apple": 2,
    "banana": 3,
    "orange": 2,
    "broccoli": 1,
    "carrot": 3,
    "tomato": 3,
    "cucumber": 2,
    "pepper": 2,
    "lemon": 2,
    "potato": 5,
    "onion": 3,
}


def load_config_credentials() -> dict:
    """
    Load credentials from ~/.config/nemlig/login.json if it exists.

    Expected format: {"username": "email@example.com", "password": "secret"}

    Returns dict with 'username' and 'password' keys, or empty dict if file doesn't exist.

    Raises:
        ValueError: If file exists but contains invalid JSON or wrong structure.
        OSError: If file exists but cannot be read.
    """
    if not CONFIG_FILE.exists():
        return {}

    with open(CONFIG_FILE, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"Config file {CONFIG_FILE} must contain a JSON object, got {type(data).__name__}"
        )

    return {
        "username": data.get("username"),
        "password": data.get("password"),
    }


def load_inventory() -> dict[str, InventoryItem]:
    """
    Load inventory from text file.

    Returns dict keyed by item name.
    """
    inventory = {}
    if not INVENTORY_FILE.exists():
        return inventory

    with open(INVENTORY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                name, quantity, last_seen = parts[0], int(parts[1]), parts[2]
                inventory[name] = InventoryItem(
                    name=name, quantity=quantity, last_seen=last_seen
                )
    return inventory


def save_inventory(inventory: dict[str, InventoryItem]) -> None:
    """Save inventory to text file."""
    INVENTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INVENTORY_FILE, "w", encoding="utf-8") as f:
        f.write("# Nemlig CLI Produce Inventory\n")
        f.write("# Format: name<TAB>quantity<TAB>last_seen\n")
        for item in sorted(inventory.values(), key=lambda x: x.name):
            f.write(f"{item.name}\t{item.quantity}\t{item.last_seen}\n")


def load_shopping_list() -> dict[str, ShoppingItem]:
    """
    Load shopping list from text file.

    Returns dict keyed by item name.
    """
    shopping_list = {}
    if not SHOPPING_LIST_FILE.exists():
        return shopping_list

    with open(SHOPPING_LIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                name, quantity, added_date = parts[0], int(parts[1]), parts[2]
                shopping_list[name] = ShoppingItem(
                    name=name, quantity=quantity, added_date=added_date
                )
    return shopping_list


def save_shopping_list(shopping_list: dict[str, ShoppingItem]) -> None:
    """Save shopping list to text file."""
    SHOPPING_LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SHOPPING_LIST_FILE, "w", encoding="utf-8") as f:
        f.write("# Nemlig CLI Shopping List\n")
        f.write("# Format: name<TAB>quantity<TAB>added_date\n")
        for item in sorted(shopping_list.values(), key=lambda x: x.name):
            f.write(f"{item.name}\t{item.quantity}\t{item.added_date}\n")


BASE_URL = "https://www.nemlig.com"
SEARCH_API_URL = "https://webapi.prod.knl.nemlig.it/searchgateway/api"


@dataclass
class AuthTokens:
    """Authentication tokens for Nemlig API."""
    xsrf_token: str
    bearer_token: str
    session: requests.Session


class ProductNotFoundError(Exception):
    """Raised when a product cannot be found by ID."""
    pass


# Order status codes from the API
ORDER_STATUS_MAP = {
    1: "Pending",
    2: "Processing",
    4: "Delivered",
}


# Maximum orders to scan when looking up by ID
MAX_ORDER_HISTORY_LOOKUP = 100


def get_common_headers() -> dict:
    """Return common headers used for all API requests."""
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Device-Size": "desktop",
        "Platform": "web",
        "Version": "11.201.0",
        "X-Correlation-Id": str(uuid.uuid4()),
    }


def login(username: str, password: str) -> AuthTokens:
    """
    Authenticate with Nemlig.com using the 3-step login flow.

    1. Get XSRF token
    2. Get Bearer token
    3. Login with credentials
    """
    session = requests.Session()
    headers = get_common_headers()

    # Step 1: Get XSRF token
    print("Step 1: Getting XSRF token...", file=sys.stderr)
    resp = session.get(f"{BASE_URL}/webapi/AntiForgery", headers=headers)
    resp.raise_for_status()
    xsrf_data = resp.json()
    xsrf_token = xsrf_data["Value"]

    # Step 2: Get Bearer token
    print("Step 2: Getting Bearer token...", file=sys.stderr)
    headers["X-Correlation-Id"] = str(uuid.uuid4())
    resp = session.get(f"{BASE_URL}/webapi/Token", headers=headers)
    resp.raise_for_status()
    token_data = resp.json()
    bearer_token = token_data["access_token"]

    # Step 3: Login
    print("Step 3: Logging in...", file=sys.stderr)
    headers["X-Correlation-Id"] = str(uuid.uuid4())
    headers["X-XSRF-TOKEN"] = xsrf_token
    headers["Authorization"] = f"Bearer {bearer_token}"
    headers["Referer"] = f"{BASE_URL}/login?returnUrl=%2F"

    login_payload = {
        "Username": username,
        "Password": password,
        "CheckForExistingProducts": True,
        "DoMerge": True,
        "AppInstalled": False,
        "SaveExistingBasket": False,
    }

    resp = session.post(f"{BASE_URL}/webapi/login", headers=headers, json=login_payload)
    resp.raise_for_status()
    login_result = resp.json()

    if "RedirectUrl" not in login_result:
        raise Exception(f"Login failed: {login_result}")

    print("Login successful!", file=sys.stderr)

    # Get fresh tokens after login
    headers["X-Correlation-Id"] = str(uuid.uuid4())
    resp = session.get(f"{BASE_URL}/webapi/Token", headers=headers)
    resp.raise_for_status()
    token_data = resp.json()
    bearer_token = token_data["access_token"]

    # Get fresh XSRF token
    resp = session.get(f"{BASE_URL}/webapi/AntiForgery", headers=headers)
    resp.raise_for_status()
    xsrf_data = resp.json()
    xsrf_token = xsrf_data["Value"]

    return AuthTokens(xsrf_token=xsrf_token, bearer_token=bearer_token, session=session)


def get_app_settings(auth: AuthTokens) -> dict:
    """Get app settings including timestamps needed for search."""
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token

    resp = auth.session.get(f"{BASE_URL}/webapi/v2/AppSettings/Website", headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_page_settings(auth: AuthTokens) -> dict:
    """Get page settings including timeslot info needed for search."""
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token

    # First get app settings to get initial timestamp
    settings = get_app_settings(auth)
    timeslot_utc = "2025120216-180-1020"  # Default fallback

    # Get page JSON which contains timeslot info
    params = {"GetAsJson": "1", "d": "1"}
    resp = auth.session.get(f"{BASE_URL}/", headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()

    page_settings = data.get("Settings", {})
    if page_settings.get("TimeslotUtc"):
        timeslot_utc = page_settings["TimeslotUtc"]

    return {
        "timestamp": settings.get("CombinedProductsAndSitecoreTimestamp", ""),
        "timeslotUtc": timeslot_utc,
        "deliveryZoneId": page_settings.get("DeliveryZoneId", 1),
        "userId": page_settings.get("UserId", ""),
    }


def search_products(auth: AuthTokens, query: str, limit: int = 10) -> list:
    """
    Search for products on nemlig.com using the full search API.

    Returns a list of product dictionaries.
    """
    page_settings = get_page_settings(auth)

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {auth.bearer_token}",
        "X-Correlation-Id": str(uuid.uuid4()),
        "Referer": f"{BASE_URL}/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }

    params = {
        "query": query,
        "take": limit,
        "skip": 0,
        "recipeCount": 0,
        "timestamp": page_settings["timestamp"],
        "timeslotUtc": page_settings["timeslotUtc"],
        "deliveryZoneId": page_settings["deliveryZoneId"],
    }

    # Add user favorites if logged in
    if page_settings.get("userId"):
        params["includeFavorites"] = page_settings["userId"]

    resp = auth.session.get(f"{SEARCH_API_URL}/search", headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()

    # Full search returns products in Products.Products structure
    products_data = data.get("Products", {})
    products = products_data.get("Products", [])
    return products


def get_basket(auth: AuthTokens) -> dict:
    """Get the current shopping basket."""
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token

    resp = auth.session.get(f"{BASE_URL}/webapi/basket/GetBasket", headers=headers)
    resp.raise_for_status()
    return resp.json()


def add_to_basket(auth: AuthTokens, product_id: str, quantity: int = 1) -> dict:
    """Add a product to the basket."""
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token
    headers["Referer"] = f"{BASE_URL}/"

    payload = {
        "ProductId": product_id,
        "quantity": quantity,
        "AffectPartialQuantity": False,
        "disableQuantityValidation": False,
    }

    resp = auth.session.post(f"{BASE_URL}/webapi/basket/AddToBasket", headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


def get_order_history(auth: AuthTokens, skip: int = 0, take: int = 10) -> dict:
    """Get paginated list of past orders."""
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token

    params = {"skip": skip, "take": take}
    resp = auth.session.get(
        f"{BASE_URL}/webapi/order/GetBasicOrderHistory", headers=headers, params=params
    )
    resp.raise_for_status()
    return resp.json()


def get_order_details(auth: AuthTokens, order_id: int) -> dict:
    """Get detailed line items for a specific order."""
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token

    resp = auth.session.get(
        f"{BASE_URL}/webapi/v2/order/GetOrderHistory/{order_id}", headers=headers
    )
    resp.raise_for_status()
    return resp.json()


def get_product_details(auth: AuthTokens, product_id: str) -> dict:
    """
    Get detailed product information using the GetAsJson endpoint.

    First searches for the product to get its URL, then fetches the full details.

    Raises:
        ProductNotFoundError: If product_id is not found or details unavailable.
    """
    # First, search to get the product URL (required because URL contains product name slug)
    products = search_products(auth, product_id, limit=5)

    # Find the exact product by ID
    product_url = None
    for p in products:
        if p.get("Id") == product_id:
            product_url = p.get("Url")
            break

    if not product_url:
        raise ProductNotFoundError(
            f"Product {product_id} not found. "
            f"Search returned {len(products)} products but none matched ID."
        )

    # Get page settings for timeslot
    page_settings = get_page_settings(auth)

    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token

    params = {
        "GetAsJson": "1",
        "t": page_settings["timeslotUtc"],
        "d": "1",
    }

    resp = auth.session.get(f"{BASE_URL}/{product_url}", headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()

    # Extract product details from content array
    content = data.get("content", [])
    for item in content:
        if item.get("TemplateName") == "productdetailspot":
            return item

    template_names = [item.get("TemplateName", "unknown") for item in content]
    raise ProductNotFoundError(
        f"Product {product_id}: No 'productdetailspot' in response. "
        f"Found templates: {template_names}"
    )


def strip_html_tags(html: str) -> str:
    """Remove HTML tags from text, returning plain text."""
    return re.sub(r"<[^>]+>", "", html).strip()


def wrap_text(text: str, width: int = 80, indent: str = "  ") -> list[str]:
    """Wrap text to specified width with indentation."""
    lines = []
    words = text.split()
    current_line = indent

    for word in words:
        if len(current_line) + len(word) + 1 > width:
            lines.append(current_line)
            current_line = indent + word
        else:
            if current_line == indent:
                current_line += word
            else:
                current_line += " " + word

    if current_line.strip():
        lines.append(current_line)

    return lines


def format_product(product: dict) -> str:
    """Format a product for display."""
    price = product.get("Price", 0)
    name = product.get("Name", "Unknown")
    brand = product.get("Brand", "")
    description = product.get("Description", "")
    product_id = product.get("Id", "")
    image_url = product.get("PrimaryImage", "")
    available = product.get("Availability", {}).get("IsAvailableInStock", False)

    availability_str = "In stock" if available else "OUT OF STOCK"

    line = f"  [{product_id}] {name} ({brand}) - {price:.2f} kr - {description} [{availability_str}]"
    if image_url:
        line += f"\n    Image: {image_url}"
    return line


def format_basket_line(line: dict) -> str:
    """Format a basket line item for display."""
    name = line.get("Name", "Unknown")
    brand = line.get("Brand", "")
    quantity = line.get("Quantity", 0)
    item_price = line.get("ItemPrice", 0)
    total_price = line.get("Price", 0)
    product_id = line.get("Id", "")

    return f"  [{product_id}] {name} ({brand}) x{quantity} @ {item_price:.2f} kr = {total_price:.2f} kr"


def format_order_summary(order: dict) -> str:
    """Format an order for the history list view."""
    order_num = order.get("OrderNumber", "Unknown")
    order_id = order.get("Id", "")
    total = order.get("Total", 0)
    status_code = order.get("Status", 0)
    order_date = order.get("OrderDate", "")

    # Parse date for display (ISO format: 2025-11-25T06:07:18Z)
    if order_date:
        date_part = order_date.split("T")[0]
    else:
        date_part = "Unknown"

    status = ORDER_STATUS_MAP.get(status_code, f"Status {status_code}")

    # Delivery time window
    delivery_time = order.get("DeliveryTime", {})
    delivery_start = delivery_time.get("Start", "")
    delivery_end = delivery_time.get("End", "")
    if delivery_start and delivery_end:
        # Extract time part (HH:MM)
        start_time = delivery_start.split("T")[1][:5] if "T" in delivery_start else ""
        end_time = delivery_end.split("T")[1][:5] if "T" in delivery_end else ""
        delivery_date = delivery_start.split("T")[0] if "T" in delivery_start else ""
        delivery_str = f"{delivery_date} {start_time}-{end_time}"
    else:
        delivery_str = "N/A"

    return f"  [{order_id}] {order_num} - {date_part} - {total:.2f} kr - {status} - Delivery: {delivery_str}"


def format_order_line(line: dict) -> str:
    """Format an order line item for display."""
    name = line.get("ProductName", "Unknown")
    quantity = line.get("Quantity", 0)
    amount = line.get("Amount", 0)
    avg_price = line.get("AverageItemPrice", 0)
    product_num = line.get("ProductNumber", "")
    description = line.get("Description", "")
    has_campaign = line.get("HasCampaign", False)

    campaign_str = " [OFFER]" if has_campaign else ""
    return f"  [{product_num}] {name} - {description} x{quantity:.0f} @ {avg_price:.2f} kr = {amount:.2f} kr{campaign_str}"


def format_order_details(order: dict, lines: list) -> str:
    """Format full order details with line items."""
    output = []

    order_num = order.get("OrderNumber", "Unknown")
    order_id = order.get("Id", "")
    total = order.get("Total", 0)
    subtotal = order.get("SubTotal", 0)
    delivery_fee = total - subtotal

    output.append(f"Order {order_num}")
    output.append("=" * (len(f"Order {order_num}")))
    output.append("")
    output.append(f"Order ID:     {order_id}")
    output.append(f"Subtotal:     {subtotal:.2f} kr")
    output.append(f"Delivery:     {delivery_fee:.2f} kr")
    output.append(f"Total:        {total:.2f} kr")
    output.append("")
    output.append(f"Items ({len(lines)}):")

    for line in lines:
        output.append(format_order_line(line))

    # Calculate totals from lines
    line_total = sum(line.get("Amount", 0) for line in lines)
    output.append("")
    output.append(f"  Lines total: {line_total:.2f} kr")

    return "\n".join(output)


def format_product_details(product: dict) -> str:
    """Format detailed product information for display."""
    lines = []

    # Basic info
    name = product.get("Name", "Unknown")
    brand = product.get("Brand", "")
    product_id = product.get("Id", "")
    price = product.get("Price", 0)
    unit_price = product.get("UnitPriceCalc", 0)
    unit_label = product.get("UnitPriceLabel", "")
    description = product.get("Description", "")
    category = product.get("Category", "")
    subcategory = product.get("SubCategory", "")

    lines.append(f"{name}")
    lines.append(f"{'=' * len(name)}")
    lines.append("")
    lines.append(f"ID:          {product_id}")
    lines.append(f"Brand:       {brand}")
    lines.append(f"Category:    {category} > {subcategory}")
    lines.append(f"Description: {description}")
    lines.append("")
    lines.append(f"Price:       {price:.2f} kr ({unit_price:.2f} {unit_label})")

    # Campaign info
    campaign = product.get("Campaign")
    if campaign:
        campaign_type = campaign.get("Type", "")
        min_qty = campaign.get("MinQuantity", 0)
        campaign_price = campaign.get("TotalPrice", 0)
        lines.append(f"Campaign:    {min_qty} for {campaign_price:.2f} kr ({campaign_type})")

    # Availability
    availability = product.get("Availability", {})
    in_stock = availability.get("IsAvailableInStock", False)
    delivery_ok = availability.get("IsDeliveryAvailable", False)
    stock_status = "In stock" if in_stock else "OUT OF STOCK"
    delivery_status = "Available" if delivery_ok else "Not available"
    lines.append("")
    lines.append(f"Stock:       {stock_status}")
    lines.append(f"Delivery:    {delivery_status}")

    # Attributes
    attributes = product.get("Attributes", [])
    if attributes:
        lines.append("")
        lines.append("Attributes:")
        for attr in attributes:
            attr_name = attr.get("Name", "")
            attr_value = attr.get("Value", "")
            lines.append(f"  {attr_name}: {attr_value}")

    # Labels
    labels = product.get("Labels", [])
    if labels:
        lines.append("")
        lines.append(f"Labels:      {', '.join(labels)}")

    # Product description (HTML text, strip tags for CLI)
    text = product.get("Text", "")
    if text:
        clean_text = strip_html_tags(text)
        if clean_text:
            lines.append("")
            lines.append("About:")
            lines.extend(wrap_text(clean_text))

    # URL
    url = product.get("Url", "")
    if url:
        lines.append("")
        lines.append(f"URL:         {BASE_URL}/{url}")

    return "\n".join(lines)


def cmd_search(auth: AuthTokens, args: argparse.Namespace) -> int:
    """Handle the search command."""
    query = args.query
    limit = args.limit

    print(f"Searching for '{query}'...", file=sys.stderr)
    products = search_products(auth, query, limit)

    if not products:
        print(f"No products found for '{query}'")
        return 1

    print(f"\nFound {len(products)} products:\n")
    for product in products:
        print(format_product(product))

    return 0


def cmd_basket(auth: AuthTokens, args: argparse.Namespace) -> int:
    """Handle the basket command."""
    print("Fetching basket...", file=sys.stderr)
    basket = get_basket(auth)

    lines = basket.get("Lines", [])

    if not lines:
        print("Your basket is empty.")
        return 0

    print(f"\nBasket ({len(lines)} items):\n")

    total = 0
    for line in lines:
        print(format_basket_line(line))
        total += line.get("Price", 0)

    print(f"\n  Total: {total:.2f} kr")

    return 0


def cmd_add(auth: AuthTokens, args: argparse.Namespace) -> int:
    """Handle the add command."""
    product_id = args.product_id
    quantity = args.quantity

    print(f"Adding product {product_id} (quantity: {quantity}) to basket...", file=sys.stderr)

    result = add_to_basket(auth, product_id, quantity)

    # Find the added product in the result
    lines = result.get("Lines", [])
    added_line = None
    for line in lines:
        if line.get("Id") == product_id:
            added_line = line
            break

    if added_line:
        print("\nAdded to basket:")
        print(format_basket_line(added_line))
    else:
        print(f"Product {product_id} added to basket.")

    # Show basket total
    total = sum(line.get("Price", 0) for line in lines)
    print(f"\nBasket total: {total:.2f} kr ({len(lines)} items)")

    return 0


def cmd_details(auth: AuthTokens, args: argparse.Namespace) -> int:
    """Handle the details command."""
    product_id = args.product_id

    print(f"Fetching details for product {product_id}...", file=sys.stderr)

    try:
        product = get_product_details(auth, product_id)
    except ProductNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print()
    print(format_product_details(product))

    return 0


def cmd_history(auth: AuthTokens, args: argparse.Namespace) -> int:
    """Handle the history command."""
    order_id = args.order_id
    limit = args.limit

    if order_id:
        # Show details for specific order
        print(f"Fetching order {order_id}...", file=sys.stderr)

        # Get order summary from recent history
        history = get_order_history(auth, skip=0, take=MAX_ORDER_HISTORY_LOOKUP)
        orders = history.get("Orders", [])
        order = None
        for o in orders:
            if o.get("Id") == order_id:
                order = o
                break

        if not order:
            print(
                f"Order {order_id} not found in last {MAX_ORDER_HISTORY_LOOKUP} orders.",
                file=sys.stderr,
            )
            return 1

        # Get line items
        details = get_order_details(auth, order_id)
        lines = details.get("Lines", [])

        print()
        print(format_order_details(order, lines))
    else:
        # List recent orders
        print("Fetching order history...", file=sys.stderr)
        history = get_order_history(auth, skip=0, take=limit)
        orders = history.get("Orders", [])
        num_pages = history.get("NumberOfPages", 1)

        if not orders:
            print("No orders found.")
            return 0

        print(f"\nOrder History ({len(orders)} orders, {num_pages} pages total):\n")
        for order in orders:
            print(format_order_summary(order))

        print("\nUse 'history ORDER_ID' to see order details.")

    return 0


# =============================================================================
# Camera and Inventory Commands
# =============================================================================


def capture_and_detect(
    model_path: str = DEFAULT_MODEL,
    timeout: float = 5.0,
    min_confidence: float = 0.5,
) -> list[Detection]:
    """
    Capture single frame from AI Camera and run object detection.

    Returns list of Detection objects for produce items only.
    """
    if not CAMERA_AVAILABLE:
        raise RuntimeError("Camera support not available. Install picamera2.")

    model_path_obj = Path(model_path)
    if not model_path_obj.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    imx500 = IMX500(model_path)
    picam2 = Picamera2(imx500.camera_num)

    config = picam2.create_still_configuration(buffer_count=2)
    picam2.start(config)

    detections = []
    start_time = time.time()

    try:
        while time.time() - start_time < timeout:
            metadata = picam2.capture_metadata()
            np_outputs = imx500.get_outputs(metadata, add_batch=True)

            if np_outputs is not None:
                # YOLOv8 post-processed outputs: boxes, scores, classes
                boxes = np_outputs[0][0]
                scores = np_outputs[1][0]
                classes = np_outputs[2][0]

                for box, score, class_id in zip(boxes, scores, classes):
                    if score >= min_confidence:
                        class_id_int = int(class_id)
                        if class_id_int in COCO_PRODUCE_CLASSES:
                            label = COCO_PRODUCE_CLASSES[class_id_int]
                            detections.append(
                                Detection(
                                    label=label,
                                    confidence=float(score),
                                    box=tuple(box),
                                )
                            )
                break

            time.sleep(0.1)
    finally:
        picam2.stop()

    return detections


def run_preview_mode(model_path: str, min_confidence: float) -> list[Detection]:
    """
    Run live camera preview with detection overlays.

    Press 'q' to quit, 'c' to capture current detections.
    Returns the detections when user presses 'c', or empty list on 'q'.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("Error: OpenCV required for preview mode.", file=sys.stderr)
        print("Install with: sudo apt install python3-opencv", file=sys.stderr)
        return []

    imx500 = IMX500(model_path)
    picam2 = Picamera2(imx500.camera_num)

    # Configure for preview with larger size
    config = picam2.create_preview_configuration(
        main={"size": (640, 480), "format": "RGB888"},
        buffer_count=4
    )
    picam2.start(config)

    print("\n[Preview Mode]")
    print("  Press 'c' to capture and use current detections")
    print("  Press 'q' to quit without saving")
    print()

    captured_detections = []
    window_name = "Nemlig Produce Scanner"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    try:
        while True:
            # Capture frame
            frame = picam2.capture_array()
            metadata = picam2.capture_metadata()

            # Get detections
            np_outputs = imx500.get_outputs(metadata, add_batch=True)
            current_detections = []

            if np_outputs is not None:
                boxes = np_outputs[0][0]
                scores = np_outputs[1][0]
                classes = np_outputs[2][0]

                for box, score, class_id in zip(boxes, scores, classes):
                    if score >= min_confidence:
                        class_id_int = int(class_id)
                        if class_id_int in COCO_PRODUCE_CLASSES:
                            label = COCO_PRODUCE_CLASSES[class_id_int]
                            current_detections.append(
                                Detection(label=label, confidence=float(score), box=tuple(box))
                            )

                            # Draw bounding box on frame
                            h, w = frame.shape[:2]
                            x1, y1, x2, y2 = box
                            # Convert normalized coords if needed (depends on model output)
                            if x2 <= 1.0:  # Normalized coordinates
                                x1, y1, x2, y2 = int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
                            else:
                                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)

                            # Draw box and label
                            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                            label_text = f"{label}: {score:.2f}"
                            cv2.putText(frame, label_text, (x1, y1 - 10),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            # Show detection count
            count_text = f"Detected: {len(current_detections)} produce items"
            cv2.putText(frame, count_text, (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.putText(frame, "Press 'c' to capture, 'q' to quit", (10, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

            # Display frame
            cv2.imshow(window_name, cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))

            # Handle key presses
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                print("Cancelled.")
                break
            elif key == ord('c'):
                captured_detections = current_detections
                print(f"Captured {len(captured_detections)} detections.")
                break

    finally:
        cv2.destroyAllWindows()
        picam2.stop()

    return captured_detections


def cmd_scan(args: argparse.Namespace) -> int:
    """Handle the scan command - detect produce and update inventory."""
    if not CAMERA_AVAILABLE:
        print("Error: Camera support not available.", file=sys.stderr)
        print("Install with: sudo apt install python3-picamera2 imx500-all", file=sys.stderr)
        return 1

    model_path = args.model
    if not Path(model_path).exists():
        print(f"Error: Model not found: {model_path}", file=sys.stderr)
        print("Install with: sudo apt install imx500-models", file=sys.stderr)
        return 1

    try:
        if args.preview:
            # Live preview mode
            detections = run_preview_mode(model_path, args.confidence)
        else:
            # Single capture mode
            print("Scanning for produce...", file=sys.stderr)
            detections = capture_and_detect(
                model_path=model_path,
                timeout=args.timeout,
                min_confidence=args.confidence,
            )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not detections:
        print("No produce detected.")
        return 0

    # Count detections by label
    counts: dict[str, int] = {}
    for det in detections:
        counts[det.label] = counts.get(det.label, 0) + 1

    print("\nDetected:")
    for label, count in sorted(counts.items()):
        # Find max confidence for this label
        max_conf = max(d.confidence for d in detections if d.label == label)
        print(f"  {count}x {label} (confidence: {max_conf:.2f})")

    # Show all individual detections in dry-run mode
    if args.dry_run:
        print("\n[Dry run - not updating inventory]")
        print("\nAll detections:")
        for i, det in enumerate(detections, 1):
            print(f"  {i}. {det.label} (conf: {det.confidence:.3f}, box: {det.box})")
        return 0

    # Update inventory
    inventory = load_inventory()
    now = datetime.now().isoformat(timespec="seconds")

    for label, count in counts.items():
        inventory[label] = InventoryItem(name=label, quantity=count, last_seen=now)

    save_inventory(inventory)
    print("\nUpdated inventory.")

    # Check for low stock
    low_stock = []
    for label, count in counts.items():
        threshold = RESTOCK_THRESHOLDS.get(label, 2)
        if count < threshold:
            low_stock.append((label, count, threshold))

    if low_stock:
        print("\nLow stock alert:")
        for label, count, threshold in low_stock:
            print(f"  {label} ({count} detected, threshold {threshold})")

        # Ask to add to shopping list
        response = input("\nAdd low stock items to shopping list? [y/N]: ").strip().lower()
        if response == "y":
            shopping_list = load_shopping_list()
            now = datetime.now().isoformat(timespec="seconds")
            for label, count, threshold in low_stock:
                needed = threshold - count + 1  # Order a bit extra
                shopping_list[label] = ShoppingItem(
                    name=label, quantity=needed, added_date=now
                )
            save_shopping_list(shopping_list)
            print("Added to shopping list.")

    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    """Handle the inventory command."""
    action = args.inventory_action

    if action == "show":
        inventory = load_inventory()
        if not inventory:
            print("Inventory is empty.")
            return 0

        print("\nProduce Inventory:\n")
        for item in sorted(inventory.values(), key=lambda x: x.name):
            print(f"  {item.name:<12} {item.quantity:>3}    (seen: {item.last_seen})")
        return 0

    elif action == "add":
        item_name = args.item.lower()
        quantity = args.quantity

        inventory = load_inventory()
        now = datetime.now().isoformat(timespec="seconds")

        if item_name in inventory:
            inventory[item_name].quantity += quantity
            inventory[item_name].last_seen = now
        else:
            inventory[item_name] = InventoryItem(
                name=item_name, quantity=quantity, last_seen=now
            )

        save_inventory(inventory)
        print(f"Added: {item_name} x{quantity}")
        return 0

    elif action == "remove":
        item_name = args.item.lower()
        quantity = args.quantity

        inventory = load_inventory()

        if item_name not in inventory:
            print(f"Error: '{item_name}' not in inventory.", file=sys.stderr)
            return 1

        if quantity >= inventory[item_name].quantity:
            del inventory[item_name]
            print(f"Removed: {item_name} (all)")
        else:
            inventory[item_name].quantity -= quantity
            print(f"Removed: {item_name} x{quantity} (remaining: {inventory[item_name].quantity})")

        save_inventory(inventory)
        return 0

    elif action == "clear":
        if INVENTORY_FILE.exists():
            INVENTORY_FILE.unlink()
        print("Inventory cleared.")
        return 0

    return 1


def cmd_shopping_list(auth: AuthTokens | None, args: argparse.Namespace) -> int:
    """Handle the shopping-list command."""
    action = args.shopping_action

    if action == "show":
        shopping_list = load_shopping_list()
        if not shopping_list:
            print("Shopping list is empty.")
            return 0

        print("\nShopping List:\n")
        for item in sorted(shopping_list.values(), key=lambda x: x.name):
            print(f"  {item.name:<12} {item.quantity:>3}")
        return 0

    elif action == "add":
        item_name = args.item.lower()
        quantity = args.quantity

        shopping_list = load_shopping_list()
        now = datetime.now().isoformat(timespec="seconds")

        if item_name in shopping_list:
            shopping_list[item_name].quantity += quantity
        else:
            shopping_list[item_name] = ShoppingItem(
                name=item_name, quantity=quantity, added_date=now
            )

        save_shopping_list(shopping_list)
        print(f"Added to shopping list: {item_name} x{quantity}")
        return 0

    elif action == "remove":
        item_name = args.item.lower()

        shopping_list = load_shopping_list()

        if item_name not in shopping_list:
            print(f"Error: '{item_name}' not in shopping list.", file=sys.stderr)
            return 1

        del shopping_list[item_name]
        save_shopping_list(shopping_list)
        print(f"Removed from shopping list: {item_name}")
        return 0

    elif action == "clear":
        if SHOPPING_LIST_FILE.exists():
            SHOPPING_LIST_FILE.unlink()
        print("Shopping list cleared.")
        return 0

    elif action == "to-basket":
        if auth is None:
            print("Error: Nemlig credentials required for to-basket.", file=sys.stderr)
            return 1

        shopping_list = load_shopping_list()
        if not shopping_list:
            print("Shopping list is empty.")
            return 0

        print("Searching nemlig.com...\n", file=sys.stderr)

        added_count = 0
        items_to_remove = []

        for item in sorted(shopping_list.values(), key=lambda x: x.name):
            # Get Danish search term
            search_term = PRODUCE_TO_NEMLIG.get(item.name, item.name)

            print(f"{item.name} -> \"{search_term}\":")

            try:
                products = search_products(auth, search_term, limit=3)
            except Exception as e:
                print(f"  Error searching: {e}")
                continue

            if not products:
                print("  No products found.")
                continue

            # Display options
            for i, product in enumerate(products, 1):
                name = product.get("Name", "Unknown")
                price = product.get("Price", 0)
                product_id = product.get("Id", "")
                print(f"  [{i}] {name} ({price:.2f} kr) [ID: {product_id}]")

            # Get user selection
            selection = input(f"Select [1-{len(products)}, s=skip]: ").strip().lower()

            if selection == "s" or not selection:
                print("  Skipped.")
                continue

            try:
                idx = int(selection) - 1
                if 0 <= idx < len(products):
                    selected = products[idx]
                    product_id = selected.get("Id")
                    quantity = item.quantity

                    result = add_to_basket(auth, product_id, quantity)
                    print(f"  Added to basket: {selected.get('Name')} x{quantity}")
                    added_count += 1
                    items_to_remove.append(item.name)
                else:
                    print("  Invalid selection, skipped.")
            except ValueError:
                print("  Invalid input, skipped.")

            print()

        # Remove added items from shopping list
        if items_to_remove:
            for name in items_to_remove:
                del shopping_list[name]
            save_shopping_list(shopping_list)

        print(f"Done! {added_count} item(s) added to basket.")
        return 0

    return 1


def main():
    parser = argparse.ArgumentParser(
        description="Nemlig.com CLI - Command-line interface for grocery shopping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Credentials:
  Credentials are loaded from ~/.config/nemlig/login.json if it exists.
  CLI options (-u, -p) override the config file values.

  Config file format:
    {"username": "email@example.com", "password": "secret"}

Examples:
  %(prog)s search "cocio"
  %(prog)s details 701025
  %(prog)s basket
  %(prog)s add 701025 --quantity 2
  %(prog)s history
  %(prog)s history 12345678

  Inventory and camera commands (no login required):
  %(prog)s scan                        # Detect produce with AI camera
  %(prog)s inventory show              # Show produce inventory
  %(prog)s inventory add apple -q 3    # Manually add items
  %(prog)s shopping-list show          # Show shopping list
  %(prog)s shopping-list to-basket     # Add items to nemlig basket (requires login)

  With explicit credentials:
  %(prog)s -u EMAIL -p PASS search "cocio"
        """
    )

    parser.add_argument("-u", "--username", help="Nemlig.com email/username (overrides config file)")
    parser.add_argument("-p", "--password", help="Nemlig.com password (overrides config file)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Search command
    search_parser = subparsers.add_parser("search", help="Search for products")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("-l", "--limit", type=int, default=10, help="Max results (default: 10)")

    # Details command
    details_parser = subparsers.add_parser("details", help="Show detailed product info")
    details_parser.add_argument("product_id", help="Product ID to view")

    # Basket command
    subparsers.add_parser("basket", help="Show current basket")

    # Add command
    add_parser = subparsers.add_parser("add", help="Add product to basket")
    add_parser.add_argument("product_id", help="Product ID to add")
    add_parser.add_argument("-q", "--quantity", type=int, default=1, help="Quantity (default: 1)")

    # History command
    history_parser = subparsers.add_parser("history", help="Show order history")
    history_parser.add_argument("order_id", nargs="?", type=int, help="Order ID for details (optional)")
    history_parser.add_argument("-l", "--limit", type=int, default=10, help="Max orders to show (default: 10)")

    # Scan command (AI camera produce detection)
    scan_parser = subparsers.add_parser("scan", help="Scan produce with AI camera")
    scan_parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Model path (default: {DEFAULT_MODEL})"
    )
    scan_parser.add_argument(
        "--timeout", type=float, default=5.0, help="Detection timeout in seconds (default: 5)"
    )
    scan_parser.add_argument(
        "--confidence", type=float, default=0.5, help="Minimum confidence threshold (default: 0.5)"
    )
    scan_parser.add_argument(
        "--preview", action="store_true", help="Show live camera preview with detections (dev mode)"
    )
    scan_parser.add_argument(
        "--dry-run", action="store_true", help="Show detections without updating inventory"
    )

    # Inventory command
    inventory_parser = subparsers.add_parser("inventory", help="Manage produce inventory")
    inventory_subparsers = inventory_parser.add_subparsers(dest="inventory_action", required=True)

    inventory_subparsers.add_parser("show", help="Show current inventory")

    inv_add_parser = inventory_subparsers.add_parser("add", help="Add item to inventory")
    inv_add_parser.add_argument("item", help="Item name (e.g., apple, tomato)")
    inv_add_parser.add_argument("-q", "--quantity", type=int, default=1, help="Quantity (default: 1)")

    inv_remove_parser = inventory_subparsers.add_parser("remove", help="Remove item from inventory")
    inv_remove_parser.add_argument("item", help="Item name to remove")
    inv_remove_parser.add_argument("-q", "--quantity", type=int, default=999, help="Quantity to remove (default: all)")

    inventory_subparsers.add_parser("clear", help="Clear all inventory")

    # Shopping list command
    shopping_parser = subparsers.add_parser("shopping-list", help="Manage shopping list")
    shopping_subparsers = shopping_parser.add_subparsers(dest="shopping_action", required=True)

    shopping_subparsers.add_parser("show", help="Show shopping list")

    shop_add_parser = shopping_subparsers.add_parser("add", help="Add item to shopping list")
    shop_add_parser.add_argument("item", help="Item name (e.g., apple, tomato)")
    shop_add_parser.add_argument("-q", "--quantity", type=int, default=1, help="Quantity (default: 1)")

    shop_remove_parser = shopping_subparsers.add_parser("remove", help="Remove item from shopping list")
    shop_remove_parser.add_argument("item", help="Item name to remove")

    shopping_subparsers.add_parser("clear", help="Clear shopping list")
    shopping_subparsers.add_parser("to-basket", help="Add shopping list items to nemlig basket")

    args = parser.parse_args()

    # Commands that don't require authentication
    NO_AUTH_COMMANDS = {"scan", "inventory"}
    # shopping-list only needs auth for to-basket action
    needs_auth = args.command not in NO_AUTH_COMMANDS
    if args.command == "shopping-list" and args.shopping_action != "to-basket":
        needs_auth = False

    # Load credentials if needed
    auth = None
    if needs_auth:
        try:
            config_creds = load_config_credentials()
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        username = args.username or config_creds.get("username")
        password = args.password or config_creds.get("password")

        if not username or not password:
            missing = []
            if not username:
                missing.append("username")
            if not password:
                missing.append("password")

            if CONFIG_FILE.exists() and config_creds:
                hint = f"Config file {CONFIG_FILE} missing {', '.join(missing)}."
            elif CONFIG_FILE.exists():
                hint = f"Config file {CONFIG_FILE} failed to load."
            else:
                hint = f"No config file at {CONFIG_FILE}."

            print(
                f"Error: Missing {' and '.join(missing)}. {hint} "
                f"Provide via config file or -u/-p options.",
                file=sys.stderr,
            )
            return 1

        try:
            auth = login(username, password)
        except requests.exceptions.HTTPError as e:
            print(f"HTTP Error: {e}", file=sys.stderr)
            if e.response is not None:
                print(f"Response: {e.response.text}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    try:
        # Execute command
        if args.command == "search":
            return cmd_search(auth, args)
        elif args.command == "details":
            return cmd_details(auth, args)
        elif args.command == "basket":
            return cmd_basket(auth, args)
        elif args.command == "add":
            return cmd_add(auth, args)
        elif args.command == "history":
            return cmd_history(auth, args)
        elif args.command == "scan":
            return cmd_scan(args)
        elif args.command == "inventory":
            return cmd_inventory(args)
        elif args.command == "shopping-list":
            return cmd_shopping_list(auth, args)
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            return 1

    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}", file=sys.stderr)
        if e.response is not None:
            print(f"Response: {e.response.text}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
