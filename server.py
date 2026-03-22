#!/usr/bin/env python3
"""
Standalone meal planner server — replaces n8n + Docker entirely.

Serves meal-planner.html and handles the two webhook endpoints:
  POST /webhook/meal-plan         — AI meal planning + nemlig product search
  POST /webhook/meal-plan-approve — basket management (add/clear/view)

Required env vars: NEMLIG_USER, NEMLIG_PASS, API_TOKEN
Optional env vars: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_MODEL
"""
import http.server
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

import requests

# ── Configuration (all from env vars) ────────────────────────
NEMLIG_USER = os.environ.get("NEMLIG_USER", "")
NEMLIG_PASS = os.environ.get("NEMLIG_PASS", "")
API_TOKEN = os.environ.get("API_TOKEN", "")

AZURE_ENDPOINT = os.environ.get("AZURE_ENDPOINT", "") or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_API_KEY = os.environ.get("AZURE_API_KEY", "") or os.environ.get("AZURE_OPENAI_KEY", "")
AZURE_DEPLOYMENT = os.environ.get("AZURE_DEPLOYMENT", "") or os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o")

NEMLIG_BASE = "https://www.nemlig.com"
SEARCH_API = "https://webapi.prod.knl.nemlig.it/searchgateway/api/search"


# ── Nemlig Auth ──────────────────────────────────────────────

def _common_headers():
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Device-Size": "desktop",
        "Platform": "web",
        "Version": "11.201.0",
        "X-Correlation-Id": str(uuid.uuid4()),
    }


def nemlig_login():
    """Authenticate with nemlig.com. Returns dict with session, bearer, xsrf, search_params."""
    s = requests.Session()
    h = _common_headers()

    # XSRF token
    r = s.get(f"{NEMLIG_BASE}/webapi/AntiForgery", headers=h)
    r.raise_for_status()
    xsrf = r.json()["Value"]

    # Bearer token
    h["X-Correlation-Id"] = str(uuid.uuid4())
    r = s.get(f"{NEMLIG_BASE}/webapi/Token", headers=h)
    r.raise_for_status()
    bearer = r.json()["access_token"]

    # Login
    h["X-Correlation-Id"] = str(uuid.uuid4())
    h["X-XSRF-TOKEN"] = xsrf
    h["Authorization"] = f"Bearer {bearer}"
    h["Referer"] = f"{NEMLIG_BASE}/login?returnUrl=%2F"
    r = s.post(f"{NEMLIG_BASE}/webapi/login", headers=h, json={
        "Username": NEMLIG_USER,
        "Password": NEMLIG_PASS,
        "CheckForExistingProducts": True,
        "DoMerge": True,
        "AppInstalled": False,
        "SaveExistingBasket": False,
    })
    r.raise_for_status()
    if "RedirectUrl" not in r.json():
        raise Exception(f"Login failed: {str(r.json())[:300]}")

    # Fresh tokens
    h["X-Correlation-Id"] = str(uuid.uuid4())
    r = s.get(f"{NEMLIG_BASE}/webapi/Token", headers=h)
    r.raise_for_status()
    bearer = r.json()["access_token"]

    r = s.get(f"{NEMLIG_BASE}/webapi/AntiForgery", headers=h)
    r.raise_for_status()
    xsrf = r.json()["Value"]

    # Search params
    timestamp = ""
    try:
        h["X-Correlation-Id"] = str(uuid.uuid4())
        h["Authorization"] = f"Bearer {bearer}"
        h["X-XSRF-TOKEN"] = xsrf
        r = s.get(f"{NEMLIG_BASE}/webapi/v2/AppSettings/Website", headers=h)
        if r.ok:
            timestamp = r.json().get("CombinedProductsAndSitecoreTimestamp", "")
    except Exception:
        pass

    tomorrow = datetime.now() + timedelta(days=1)
    timeslot_utc = f"{tomorrow.strftime('%Y%m%d')}16-60-180"

    return {
        "session": s,
        "bearer": bearer,
        "xsrf": xsrf,
        "search_params": {
            "timestamp": timestamp,
            "timeslotUtc": timeslot_utc,
            "deliveryZoneId": 1,
        },
    }


def _auth_headers(auth):
    h = _common_headers()
    h["Authorization"] = f"Bearer {auth['bearer']}"
    h["X-XSRF-TOKEN"] = auth["xsrf"]
    h["Referer"] = f"{NEMLIG_BASE}/"
    return h


# ── LLM Call ─────────────────────────────────────────────────

def call_llm(system_prompt, user_message, llm_config):
    """Call LLM with provider routing. Returns the raw content string."""
    cfg = llm_config or {}
    provider = cfg.get("provider") or "azure"
    endpoint = cfg.get("endpoint") or ""
    model = cfg.get("model") or ""
    api_key = cfg.get("apiKey") or ""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    if provider == "azure":
        ep = endpoint or AZURE_ENDPOINT
        key = api_key or AZURE_API_KEY
        mod = model or AZURE_DEPLOYMENT
        if not ep or not key:
            raise Exception("Azure OpenAI not configured. Set AZURE_ENDPOINT and AZURE_API_KEY env vars, or configure LLM settings in the UI.")
        url = f"{ep}/openai/deployments/{mod}/chat/completions?api-version=2025-04-01-preview"
        headers = {"Content-Type": "application/json", "api-key": key}
        body = {"messages": messages, "temperature": 0.7, "max_completion_tokens": 4000, "response_format": {"type": "json_object"}}

    elif provider == "openai":
        url = f"{endpoint or 'https://api.openai.com'}/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        body = {"model": model or "gpt-4o", "messages": messages, "temperature": 0.7, "max_tokens": 4000, "response_format": {"type": "json_object"}}

    elif provider == "anthropic":
        url = f"{endpoint or 'https://api.anthropic.com'}/v1/messages"
        headers = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body = {"model": model or "claude-sonnet-4-20250514", "max_tokens": 4000, "system": system_prompt, "messages": [{"role": "user", "content": user_message}]}

    elif provider == "ollama":
        url = f"{endpoint or 'http://localhost:11434'}/api/chat"
        headers = {"Content-Type": "application/json"}
        body = {"model": model or "llama3", "messages": messages, "stream": False, "format": "json"}

    else:  # custom OpenAI-compatible
        url = f"{endpoint}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model or "default", "messages": messages, "temperature": 0.7, "max_tokens": 4000}

    r = requests.post(url, json=body, headers=headers, timeout=120)
    if r.status_code >= 400:
        raise Exception(f"LLM API error ({provider}): {r.status_code} — {r.text[:500]}")

    data = r.json()
    if provider == "anthropic":
        return data.get("content", [{}])[0].get("text", "")
    elif provider == "ollama":
        return data.get("message", {}).get("content", "")
    else:
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")


# ── Prompt Builder ───────────────────────────────────────────

SYSTEM_PROMPT = """You are a Danish grocery meal planner for nemlig.com. You create weekly meal plans with recipes and ingredient lists.

You are an expert on dietary restrictions, food science, AND Danish grocery prices.

CRITICAL BUDGET RULES:
- The user has a STRICT budget. You MUST plan meals that fit within it.
- Typical nemlig.com prices: chicken breast ~50kr/pack, minced meat ~35kr, eggs ~30-40kr, rice ~15-20kr, pasta ~15-25kr, vegetables ~10-25kr each, milk ~12kr, butter ~28kr
- For a 500kr weekly budget for 2 people, plan simple meals with 8-12 total ingredients
- For 800kr, you can have 12-18 ingredients
- NEVER exceed the budget. If in doubt, plan fewer meals or simpler recipes.
- ORGANIC RULE: If the user mentions organic/økologisk, you MUST append "øko" to EVERY search term (e.g. "kyllingebrystfilet øko", "gulerødder øko", "æg øko", "basmatiris øko", "hakket oksekød øko"). No exceptions — every single ingredient search term must end with "øko".
- Even with organic, stay within budget — organic staples are usually only 10-30% more expensive

SEARCH TERM RULES:
- All search terms MUST be specific Danish product names as you'd type in a grocery search
- GOOD: "kyllingebrystfilet", "hakket kalkun", "hakket oksekød", "basmatiris", "torskefilet", "æg", "spaghetti"
- BAD: "kalkun" (returns whole turkey!), "kød" (too vague), "fisk" (too vague), "ris" (too generic)
- Always use the specific cut/type: "hakket" (minced), "filet", "bryst" (breast), "strimler" (strips)

QUANTITY RULES:
- Quantity = number of PACKAGES to buy from the store (1-3 max per ingredient)
- Most items need quantity 1. Only staples like rice/pasta might need 2.
- Consolidate: if multiple meals use chicken, list it ONCE with combined quantity

OTHER RULES:
- Scale for the number of people
- Respect ALL dietary restrictions strictly
- Be practical: use common Danish supermarket ingredients
- Include a BILINGUAL recipe for each meal. Each entry in the recipe array is ONE step containing BOTH languages separated by a newline. Format: "DK: Danish instruction\\nEN: English translation". The step NUMBER is only shown once at render time — do NOT include step numbers in the text. Example: ["DK: Skær løg i tern og steg i olie.\\nEN: Dice the onion and fry in oil.", "DK: Tilsæt hvidløg og steg i 1 minut.\\nEN: Add garlic and fry for 1 minute."]
- Use as many or few steps as the dish actually needs — a simple salad might be 2 steps, a stew might be 8. Do NOT pad or compress to hit a fixed number.
- Write each step as a clear instruction a home cook can follow, with temps, times, and quantities where relevant.

Respond with valid JSON only. No markdown, no explanation."""


def build_prompt(form_data):
    """Build system + user prompts from form data. Returns (system_prompt, user_message, wants_organic)."""
    meals = form_data.get("meals", "")
    people = form_data.get("people", 2)
    days = form_data.get("days", "Monday, Tuesday, Wednesday, Thursday, Friday")
    budget = form_data.get("budget", 500)
    diet = form_data.get("diet", "")
    notes = form_data.get("notes", "")

    combined = f"{meals} {notes} {diet}"
    wants_organic = bool(re.search(r"organic|økologisk|øko", combined, re.IGNORECASE))

    user_msg = f"Plan meals for {people} people.\nDays: {days}\nMeal ideas: {meals}\nBudget: {budget} kr (STRICT - do not exceed!)"
    if wants_organic:
        user_msg += "\nIMPORTANT: User wants ORGANIC — append øko to ALL search terms!"
    if diet:
        user_msg += f"\nDiet / restrictions: {diet}"
    if notes:
        user_msg += f"\nExtra notes: {notes}"

    user_msg += """

Return a JSON object with this exact structure:
{
  "mealPlan": [
    {
      "day": "Monday",
      "meals": [
        {
          "type": "dinner",
          "name": "Meal name",
          "description": "Brief description",
          "recipe": ["DK: Trin 1 på dansk...\\nEN: Step 1 in English...", "DK: Trin 2 på dansk...\\nEN: Step 2 in English..."]
        }
      ]
    }
  ],
  "ingredients": [
    { "searchTerm": "specific Danish search term", "quantity": 1, "category": "protein/dairy/vegetable/grain/other", "displayName": "Name with weight", "estimatedPrice": 30 }
  ],
  "estimatedTotal": 350,
  "budgetNotes": "Brief note on how budget was managed"
}"""

    return SYSTEM_PROMPT, user_msg, wants_organic


# ── AI Response Parser ───────────────────────────────────────

def parse_ai_response(content):
    """Parse and validate the LLM JSON response."""
    # Strip markdown code fences if present
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        raise Exception(f"AI returned invalid JSON: {content[:500]}")

    if "mealPlan" not in plan or "ingredients" not in plan:
        raise Exception("AI response missing mealPlan or ingredients")

    ingredients = [i for i in plan["ingredients"] if i.get("searchTerm", "").strip()]
    if not ingredients:
        raise Exception("No valid ingredients in AI response")

    for ing in ingredients:
        if ing.get("quantity", 1) > 3:
            ing["quantity"] = 3

    return {
        "mealPlan": plan["mealPlan"],
        "ingredients": ingredients,
        "estimatedTotal": plan.get("estimatedTotal", 0),
        "budgetNotes": plan.get("budgetNotes", ""),
    }


# ── Product Search & Budget ──────────────────────────────────

def search_and_aggregate(auth, ingredients, budget, wants_organic):
    """Search nemlig.com for ingredients and enforce budget."""
    matched = []
    not_found = []

    if wants_organic:
        for ing in ingredients:
            if not re.search(r"øko", ing["searchTerm"], re.IGNORECASE):
                ing["searchTerm"] += " øko"

    for i, ing in enumerate(ingredients):
        if i > 0:
            time.sleep(0.5)

        try:
            r = auth["session"].get(SEARCH_API, params={
                "query": ing["searchTerm"],
                "take": 30, "skip": 0, "recipeCount": 0,
                "timestamp": auth["search_params"]["timestamp"],
                "timeslotUtc": auth["search_params"]["timeslotUtc"],
                "deliveryZoneId": auth["search_params"]["deliveryZoneId"],
            }, headers={
                "Accept": "application/json, text/plain, */*",
                "Authorization": f"Bearer {auth['bearer']}",
                "Referer": "https://www.nemlig.com/",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            }, timeout=15)

            if r.status_code != 200:
                not_found.append({"searchTerm": ing["searchTerm"], "displayName": ing.get("displayName", ing["searchTerm"]), "wantedQty": ing.get("quantity", 1)})
                continue

            products = r.json().get("Products", {}).get("Products", [])
            available = [p for p in products if
                         p.get("Availability", {}).get("IsAvailableInStock") is not False and
                         p.get("Availability", {}).get("IsDeliveryAvailable") is not False]

            if not available:
                not_found.append({"searchTerm": ing["searchTerm"], "displayName": ing.get("displayName", ing["searchTerm"]), "wantedQty": ing.get("quantity", 1)})
                continue

            # Pick best product
            if wants_organic:
                organic = [p for p in available if re.search(r"øko|økologisk", p.get("Name", ""), re.IGNORECASE)]
                if organic:
                    organic.sort(key=lambda p: p.get("Price", 999))
                    best = organic[0]
                else:
                    available.sort(key=lambda p: p.get("Price", 999))
                    best = available[0]
            else:
                available.sort(key=lambda p: p.get("Price", 999))
                best = available[0]

            qty = min(ing.get("quantity", 1), 3)
            price = best.get("Price", 0)
            matched.append({
                "searchTerm": ing["searchTerm"],
                "displayName": ing.get("displayName", ing["searchTerm"]),
                "wantedQty": qty,
                "productId": best["Id"],
                "productName": best.get("Name", ""),
                "brand": best.get("Brand", ""),
                "price": price,
                "totalPrice": price * qty,
            })

        except Exception:
            not_found.append({"searchTerm": ing["searchTerm"], "displayName": ing.get("displayName", ing["searchTerm"]), "wantedQty": ing.get("quantity", 1)})

    # Deduplicate by productId
    seen = {}
    deduped = []
    for item in matched:
        pid = item["productId"]
        if pid in seen:
            existing = seen[pid]
            existing["wantedQty"] = min(existing["wantedQty"] + item["wantedQty"], 5)
            existing["totalPrice"] = existing["price"] * existing["wantedQty"]
        else:
            seen[pid] = item
            deduped.append(item)
    matched = deduped

    # Budget enforcement
    total_price = sum(m["totalPrice"] for m in matched)

    if total_price > budget:
        by_price = sorted(matched, key=lambda m: m["price"], reverse=True)
        for item in by_price:
            if total_price <= budget:
                break
            while item["wantedQty"] > 1 and total_price > budget:
                item["wantedQty"] -= 1
                item["totalPrice"] = item["price"] * item["wantedQty"]
                total_price = sum(m["totalPrice"] for m in matched)

    if total_price > budget:
        matched.sort(key=lambda m: m["price"], reverse=True)
        while len(matched) > 3 and total_price > budget:
            removed = matched.pop(0)
            not_found.append({"searchTerm": removed["searchTerm"], "displayName": removed["displayName"] + " (over budget)", "wantedQty": removed["wantedQty"]})
            total_price = sum(m["totalPrice"] for m in matched)

    return {
        "matched": matched,
        "notFound": not_found,
        "totalPrice": total_price,
        "budget": budget,
        "remaining": budget - total_price,
    }


# ── Basket Operations ───────────────────────────────────────

def handle_basket(auth, body):
    """Handle basket actions: add items, clear, or view."""
    h = _auth_headers(auth)
    s = auth["session"]
    action = body.get("action", "")

    if action == "clear":
        s.post(f"{NEMLIG_BASE}/webapi/basket/ClearBasket", headers=h, json={})
        return {"cleared": True, "added": 0, "failed": 0, "totalItems": 0}

    if action == "view":
        r = s.get(f"{NEMLIG_BASE}/webapi/basket/GetBasket", headers=h)
        r.raise_for_status()
        data = r.json()
        lines = data.get("Lines", [])
        items = [{"name": l.get("Name", ""), "qty": l.get("Quantity", 0), "price": l.get("Price", 0), "image": l.get("PrimaryImage", "")} for l in lines]
        return {"action": "view", "items": items, "count": len(lines), "total": data.get("TotalPrice", 0)}

    # Default: add items
    items = body.get("items", [])
    if not items:
        raise Exception("No items to add")

    results = []
    for i, item in enumerate(items):
        if i > 0:
            time.sleep(1)
        try:
            s.post(f"{NEMLIG_BASE}/webapi/basket/AddToBasket", headers=h, json={
                "ProductId": str(item["productId"]),
                "quantity": item.get("quantity", 1),
                "AffectPartialQuantity": False,
                "disableQuantityValidation": False,
            })
            results.append({"productId": item["productId"], "name": item.get("name", ""), "success": True})
        except Exception as e:
            results.append({"productId": item["productId"], "name": item.get("name", ""), "success": False, "error": str(e)})

    added = sum(1 for r in results if r["success"])
    failed = sum(1 for r in results if not r["success"])
    return {"results": results, "added": added, "failed": failed, "totalItems": len(items)}


# ── Token Validation ─────────────────────────────────────────

def validate_token(query_string, headers=None):
    """Check ?token= query param or X-Api-Token header against API_TOKEN."""
    if not API_TOKEN:
        return True  # no token configured = open access
    # Check query param
    params = parse_qs(query_string)
    token = params.get("token", [""])[0]
    if token:
        return token == API_TOKEN
    # Check header
    if headers:
        token = headers.get("X-Api-Token", "")
        if token:
            return token == API_TOKEN
    # No token provided by client = allow (local dev without token in URL)
    return True


# ── HTTP Handler ─────────────────────────────────────────────

class ThreadingHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class MealPlanHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Shorter log format
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Token")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/meal-planner"):
            self.path = "/meal-planner.html"
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query

        if not validate_token(query, self.headers):
            self._send_json({"error": "Unauthorized"}, 401)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if path == "/webhook/meal-plan":
            self._handle_meal_plan(body)
        elif path == "/webhook/meal-plan-approve":
            self._handle_approve(body)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_meal_plan(self, body):
        try:
            # 1. Build prompt
            system_prompt, user_message, wants_organic = build_prompt(body)
            print(f"  [meal-plan] LLM call ({body.get('llmConfig', {}).get('provider', 'azure')})...")

            # 2. Call LLM
            content = call_llm(system_prompt, user_message, body.get("llmConfig"))
            print(f"  [meal-plan] Parsing AI response...")

            # 3. Parse response
            parsed = parse_ai_response(content)
            print(f"  [meal-plan] {len(parsed['ingredients'])} ingredients, logging into nemlig...")

            # 4. Nemlig auth
            auth = nemlig_login()
            print(f"  [meal-plan] Searching products...")

            # 5. Search & budget
            results = search_and_aggregate(auth, parsed["ingredients"], body.get("budget", 500), wants_organic)
            print(f"  [meal-plan] Done: {len(results['matched'])} matched, {len(results['notFound'])} not found, {results['totalPrice']:.0f} kr")

            # 6. Respond
            self._send_json({
                "mealPlan": parsed["mealPlan"],
                "matched": results["matched"],
                "notFound": results["notFound"],
                "totalPrice": results["totalPrice"],
                "budget": results["budget"],
                "remaining": results["remaining"],
            })

        except Exception as e:
            print(f"  [meal-plan] ERROR: {e}")
            self._send_json({"error": str(e)}, 500)

    def _handle_approve(self, body):
        try:
            auth = nemlig_login()
            result = handle_basket(auth, body)
            self._send_json(result)
        except Exception as e:
            print(f"  [approve] ERROR: {e}")
            self._send_json({"error": str(e)}, 500)

    def _send_json(self, data, status=200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

    missing = []
    if not NEMLIG_USER:
        missing.append("NEMLIG_USER")
    if not NEMLIG_PASS:
        missing.append("NEMLIG_PASS")
    if missing:
        print(f"WARNING: Missing env vars: {', '.join(missing)} — nemlig.com features won't work")

    if not API_TOKEN:
        print("WARNING: API_TOKEN not set — endpoints are open (no auth)")

    print(f"Meal Planner server on http://localhost:{port}")
    print(f"  Open http://localhost:{port}/meal-planner to use the UI")
    ThreadingHTTPServer(("", port), MealPlanHandler).serve_forever()
