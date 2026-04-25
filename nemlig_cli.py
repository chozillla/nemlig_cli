#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
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
import curses
import itertools
import json
import os
import re
import readline
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import argcomplete
import requests

# Optional: OpenAI-compatible LLM backends (Azure, OpenAI, Mistral, Groq, etc.)
try:
    from openai import AzureOpenAI, OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Optional: Anthropic (Claude) backend
try:
    import anthropic as _anthropic_mod
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# Optional: Google Sheets for form responses
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GSHEETS_AVAILABLE = True
except ImportError:
    GSHEETS_AVAILABLE = False

GSHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# ── Diet/Meal Template (CLI-only for now; move to website later) ─────
MEAL_TEMPLATE_PATH = Path(__file__).resolve().parent / "meal_template.json"


def load_meal_template():
    """Load the diet template if present. Missing/invalid file is fine."""
    try:
        with open(MEAL_TEMPLATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def render_meal_template_block(tpl):
    """Convert the meal template JSON into prompt text the LLM can act on."""
    macros = tpl.get("daily_macros", {})
    foods = tpl.get("priority_foods", {})
    rules = tpl.get("rules", [])
    avoid = tpl.get("avoid", [])
    intolerances = tpl.get("intolerances", [])

    lines = [f"DIET TEMPLATE: {tpl.get('name', 'unnamed')} (goal: {tpl.get('goal', 'n/a')})"]

    if macros:
        lines.append(
            f"DAILY MACRO TARGETS (per person, per day): "
            f"{macros.get('calories', '?')} kcal | "
            f"{macros.get('protein_g', '?')}g protein | "
            f"{macros.get('carbs_g', '?')}g carbs | "
            f"{macros.get('fat_g', '?')}g fat. "
            f"Plan the week so the daily average lands within ~10% of these targets."
        )

    if foods:
        lines.append("PRIORITY FOODS (prefer these Danish nemlig.com items):")
        for category, items in foods.items():
            if items:
                lines.append(f"  - {category}: {', '.join(items)}")

    if rules:
        lines.append("DIET RULES (must follow):")
        lines.extend(f"  - {r}" for r in rules)

    if avoid:
        lines.append("AVOID:")
        lines.extend(f"  - {a}" for a in avoid)

    if intolerances:
        lines.append(f"INTOLERANCES (strict — never include): {', '.join(intolerances)}")

    return "\n".join(lines)


MEAL_TEMPLATE = load_meal_template()

# Optional: Barcode scanning and image recognition
try:
    import cv2
    from pyzbar import pyzbar
    from PIL import Image
    import openfoodfacts
    SCANNER_AVAILABLE = True
except ImportError:
    SCANNER_AVAILABLE = False

# Optional: Raspberry Pi AI Camera
try:
    from picamera2 import Picamera2
    from picamera2.devices.imx500 import IMX500
    PICAMERA_AVAILABLE = True
except ImportError:
    PICAMERA_AVAILABLE = False


# Interactive mode commands for tab completion
COMMANDS = ["search", "details", "list", "basket", "help", "quit", "exit"]
LIST_SUBCOMMANDS = ["add", "remove", "clear", "budget", "sync"]


class NemligCompleter:
    """Tab completer for interactive mode."""

    def __init__(self):
        self.matches = []

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            line = readline.get_line_buffer()
            self.matches = self._get_matches(line, text)
        return self.matches[state] if state < len(self.matches) else None

    def _get_matches(self, line: str, text: str) -> list[str]:
        parts = line.split()

        # First word - complete commands
        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            return [cmd + " " for cmd in COMMANDS if cmd.startswith(text)]

        # After "list" - complete subcommands
        if parts[0] == "list":
            if len(parts) == 1 and line.endswith(" "):
                return [sub + " " for sub in LIST_SUBCOMMANDS]
            elif len(parts) == 2 and not line.endswith(" "):
                return [sub + " " for sub in LIST_SUBCOMMANDS if sub.startswith(text)]

        return []


class Spinner:
    """Animated spinner for long-running operations."""

    def __init__(self, message: str = "Loading"):
        self.message = message
        self.running = False
        self.thread = None
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def _spin(self):
        for frame in itertools.cycle(self.frames):
            if not self.running:
                break
            print(f"\r  {frame} {self.message}...", end="", flush=True)
            time.sleep(0.08)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._spin)
        self.thread.start()

    def stop(self, final_message: str = None):
        self.running = False
        if self.thread:
            self.thread.join()
        # Clear the line
        print(f"\r{' ' * (len(self.message) + 10)}\r", end="")
        if final_message:
            print(f"  ✓ {final_message}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


VERSION = "1.0.0"

LOGO = r"""
    ░░░    ░░░  ░░░░░░░  ░░░    ░░░  ░░░      ░░░   ░░░░░░░
    ░░░░   ░░░  ░░░      ░░░░  ░░░░  ░░░      ░░░  ░░░
    ░░░░░  ░░░  ░░░░░░   ░░░░░░░░░░  ░░░      ░░░  ░░░  ░░░░
    ░░░ ░░ ░░░  ░░░      ░░░ ░░ ░░░  ░░░      ░░░  ░░░   ░░░
    ░░░  ░░░░░  ░░░░░░░  ░░░    ░░░  ░░░░░░░  ░░░   ░░░░░░░

    ─────────────────────────────────────────────────────

       ██████╗ ██╗      ██╗    grocery shopping from your terminal
      ██╔════╝ ██║      ██║    ─────────────────────────────────────
      ██║      ██║      ██║    search, list, sync - all from the cli
      ██║      ██║      ██║
       ██████╗ ███████╗ ██║    v{version}
       ╚═════╝ ╚══════╝ ╚═╝
"""


def print_welcome(username: str) -> None:
    """Print welcome banner with logo after login."""
    print(LOGO.format(version=VERSION))
    print(f"    Logged in as: {username}")
    print("    ─────────────────────────────────────────────────────\n")


def print_startup_logo() -> None:
    """Print startup logo before login."""
    print(LOGO.format(version=VERSION))
    print("    ─────────────────────────────────────────────────────\n")

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
        # Generic AI config (works with any provider)
        "ai_provider": data.get("ai_provider"),
        "ai_api_key": data.get("ai_api_key"),
        "ai_model": data.get("ai_model"),
        "ai_base_url": data.get("ai_base_url"),
        # Azure (legacy keys, still supported)
        "azure_api_key": data.get("azure_api_key"),
        "azure_endpoint": data.get("azure_endpoint"),
        "azure_deployment": data.get("azure_deployment"),
        # OpenAI (legacy keys, still supported)
        "openai_api_key": data.get("openai_api_key"),
        "openai_model": data.get("openai_model"),
        # Ollama (legacy keys, still supported)
        "ollama_base_url": data.get("ollama_base_url"),
        "ollama_model": data.get("ollama_model"),
    }


# ---------------------------------------------------------------------------
# LLM provider registry — plug-and-play backends
# ---------------------------------------------------------------------------
# Each entry maps a provider name to its base URL, default model, and the
# environment-variable prefix used for API key / model overrides.
# All providers listed here speak the OpenAI-compatible chat completions API
# (except "anthropic", which uses a lightweight adapter below).

_PROVIDER_REGISTRY: dict[str, dict] = {
    # Cloud APIs — OpenAI-compatible
    "openai":    {"base_url": None,                                       "default_model": "gpt-4o",                                              "env_prefix": "OPENAI"},
    "mistral":   {"base_url": "https://api.mistral.ai/v1",               "default_model": "mistral-large-latest",                                "env_prefix": "MISTRAL"},
    "groq":      {"base_url": "https://api.groq.com/openai/v1",          "default_model": "llama-3.3-70b-versatile",                             "env_prefix": "GROQ"},
    "together":  {"base_url": "https://api.together.xyz/v1",             "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",             "env_prefix": "TOGETHER"},
    "deepseek":  {"base_url": "https://api.deepseek.com",                "default_model": "deepseek-chat",                                       "env_prefix": "DEEPSEEK"},
    "xai":       {"base_url": "https://api.x.ai/v1",                     "default_model": "grok-3",                                              "env_prefix": "XAI"},
    "fireworks": {"base_url": "https://api.fireworks.ai/inference/v1",    "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",   "env_prefix": "FIREWORKS"},
    "openrouter":{"base_url": "https://openrouter.ai/api/v1",            "default_model": "openai/gpt-4o",                                       "env_prefix": "OPENROUTER"},
    # Local / self-hosted
    "ollama":    {"base_url": "http://localhost:11434/v1",                "default_model": "llama3.2",                                            "env_prefix": "OLLAMA",    "no_key": True},
    "lmstudio":  {"base_url": "http://localhost:1234/v1",                 "default_model": "default",                                             "env_prefix": "LMSTUDIO",  "no_key": True},
}


# ---------------------------------------------------------------------------
# Anthropic adapter — translates OpenAI chat-completions interface to the
# Anthropic messages API so callers don't need to care about the difference.
# ---------------------------------------------------------------------------

class _AnthropicCompletions:
    """Implements client.chat.completions.create() using the Anthropic SDK."""

    def __init__(self, client):
        self._client = client

    # --- public API (mirrors openai) ---

    def create(self, *, model, messages, max_completion_tokens=4096, tools=None, **_kwargs):
        system_parts, converted = self._convert_messages(messages)

        anthropic_tools = None
        if tools:
            anthropic_tools = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]

        call_kw: dict = {
            "model": model,
            "max_tokens": max_completion_tokens,
            "messages": converted,
        }
        if system_parts:
            call_kw["system"] = "\n\n".join(system_parts)
        if anthropic_tools:
            call_kw["tools"] = anthropic_tools

        resp = self._client.messages.create(**call_kw)
        return self._to_openai_response(resp)

    # --- internal helpers ---

    @staticmethod
    def _role_and_content(msg):
        if isinstance(msg, dict):
            return msg["role"], msg.get("content")
        return msg.role, getattr(msg, "content", None)

    @staticmethod
    def _tool_calls_of(msg):
        if isinstance(msg, dict):
            return msg.get("tool_calls")
        return getattr(msg, "tool_calls", None)

    def _convert_messages(self, messages):
        system_parts: list[str] = []
        converted: list[dict] = []

        for msg in messages:
            role, content = self._role_and_content(msg)

            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            if role == "tool":
                tid = msg.get("tool_call_id") if isinstance(msg, dict) else getattr(msg, "tool_call_id", "")
                block = {"type": "tool_result", "tool_use_id": tid, "content": content or ""}
                # Merge consecutive tool results into one user turn
                if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
                continue

            if role == "assistant":
                tc = self._tool_calls_of(msg)
                if tc:
                    blocks: list[dict] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for c in tc:
                        blocks.append({
                            "type": "tool_use",
                            "id": c.id,
                            "name": c.function.name,
                            "input": json.loads(c.function.arguments),
                        })
                    converted.append({"role": "assistant", "content": blocks})
                else:
                    converted.append({"role": "assistant", "content": content or ""})
                continue

            # user (or any other role)
            converted.append({"role": role, "content": content or ""})

        return system_parts, converted

    @staticmethod
    def _to_openai_response(resp):
        tool_blocks = [b for b in resp.content if b.type == "tool_use"]
        text_blocks = [b for b in resp.content if b.type == "text"]
        text = "\n".join(b.text for b in text_blocks) if text_blocks else None

        if tool_blocks:
            tool_calls = [
                SimpleNamespace(
                    id=b.id,
                    function=SimpleNamespace(name=b.name, arguments=json.dumps(b.input)),
                )
                for b in tool_blocks
            ]
            message = SimpleNamespace(content=text, tool_calls=tool_calls, role="assistant")
            finish = "tool_calls"
        else:
            message = SimpleNamespace(content=text, tool_calls=None, role="assistant")
            finish = "stop"

        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=message)])


class _AnthropicAdapter:
    """Drop-in replacement for openai.OpenAI that routes to Anthropic."""

    def __init__(self, client):
        self.chat = SimpleNamespace(completions=_AnthropicCompletions(client))


# ---------------------------------------------------------------------------
# Provider resolution + client factory
# ---------------------------------------------------------------------------

def _resolve_ai_provider(creds: dict) -> str:
    """Determine AI provider: AI_PROVIDER env > config ai_provider > auto-detect > 'azure'."""
    explicit = (os.environ.get("AI_PROVIDER", "").lower()
                or (creds.get("ai_provider") or "").lower())
    if explicit:
        return explicit

    # Auto-detect from provider-specific env vars
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    for name, info in _PROVIDER_REGISTRY.items():
        env_key = f"{info['env_prefix']}_API_KEY"
        if os.environ.get(env_key):
            return name

    # Auto-detect from config file keys
    if creds.get("ai_api_key"):
        return "openai"
    if creds.get("azure_api_key"):
        return "azure"
    if creds.get("openai_api_key"):
        return "openai"

    return "azure"


def get_ai_client() -> "tuple | None":
    """Return (client, model_name) for the configured AI provider, or None.

    Supports all providers in _PROVIDER_REGISTRY (OpenAI-compatible),
    plus Azure OpenAI, Anthropic (Claude), and fully custom endpoints.
    """
    try:
        creds = load_config_credentials()
    except Exception:
        creds = {}

    provider = _resolve_ai_provider(creds)

    # --- Azure OpenAI (special client class) ---
    if provider == "azure":
        api_key = os.environ.get("AZURE_API_KEY") or creds.get("azure_api_key") or creds.get("ai_api_key")
        endpoint = os.environ.get("AZURE_ENDPOINT") or creds.get("azure_endpoint") or "https://cehs-mk59u7e0-eastus2.cognitiveservices.azure.com/"
        model = os.environ.get("AZURE_DEPLOYMENT") or creds.get("azure_deployment") or creds.get("ai_model") or "gpt-5.2-2"
        if not api_key:
            return None
        client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version="2024-12-01-preview",
        )
        return client, model

    # --- Anthropic (adapter wraps the native SDK) ---
    if provider == "anthropic":
        if not ANTHROPIC_AVAILABLE:
            return None
        api_key = os.environ.get("ANTHROPIC_API_KEY") or creds.get("ai_api_key")
        model = os.environ.get("ANTHROPIC_MODEL") or creds.get("ai_model") or "claude-sonnet-4-5-20250929"
        if not api_key:
            return None
        client = _AnthropicAdapter(_anthropic_mod.Anthropic(api_key=api_key))
        return client, model

    # --- Custom endpoint (user provides everything) ---
    if provider == "custom":
        base_url = os.environ.get("CUSTOM_BASE_URL") or creds.get("ai_base_url")
        api_key = os.environ.get("CUSTOM_API_KEY") or creds.get("ai_api_key") or "no-key"
        model = os.environ.get("CUSTOM_MODEL") or creds.get("ai_model") or "default"
        if not base_url:
            return None
        return OpenAI(base_url=base_url, api_key=api_key), model

    # --- Registry-based providers (OpenAI-compatible) ---
    info = _PROVIDER_REGISTRY.get(provider)
    if not info:
        return None

    prefix = info["env_prefix"]
    api_key = (os.environ.get(f"{prefix}_API_KEY")
               or creds.get(f"{provider}_api_key")
               or creds.get("ai_api_key"))
    model = (os.environ.get(f"{prefix}_MODEL")
             or creds.get(f"{provider}_model")
             or creds.get("ai_model")
             or info["default_model"])
    base_url = (os.environ.get(f"{prefix}_BASE_URL")
                or creds.get(f"{provider}_base_url")
                or creds.get("ai_base_url")
                or info["base_url"])

    if not info.get("no_key") and not api_key:
        return None

    kw: dict = {"api_key": api_key or "no-key"}
    if base_url:
        kw["base_url"] = base_url
    return OpenAI(**kw), model


# Google Sheets config
GSHEETS_CONFIG_FILE = Path.home() / ".config" / "nemlig" / "gsheets.json"
GSHEETS_TOKEN_FILE = Path.home() / ".config" / "nemlig" / "gsheets_token.json"
GSHEETS_CREDENTIALS_FILE = Path.home() / ".config" / "nemlig" / "credentials.json"


def load_gsheets_config() -> dict:
    """Load Google Sheets configuration."""
    if GSHEETS_CONFIG_FILE.exists():
        return json.loads(GSHEETS_CONFIG_FILE.read_text())
    return {}


def save_gsheets_config(config: dict) -> None:
    """Save Google Sheets configuration."""
    GSHEETS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    GSHEETS_CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get_gsheets_service():
    """Get authenticated Google Sheets service."""
    if not GSHEETS_AVAILABLE:
        raise RuntimeError("Google Sheets libraries not installed. Run: uv add google-api-python-client google-auth-oauthlib")

    creds = None

    # Load existing token
    if GSHEETS_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GSHEETS_TOKEN_FILE), GSHEETS_SCOPES)

    # Refresh or get new credentials
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GSHEETS_CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Google credentials file not found at {GSHEETS_CREDENTIALS_FILE}\n"
                    "Download from Google Cloud Console: APIs & Services > Credentials > OAuth 2.0 Client IDs"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(GSHEETS_CREDENTIALS_FILE), GSHEETS_SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for next time
        GSHEETS_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GSHEETS_TOKEN_FILE.write_text(creds.to_json())

    return build("sheets", "v4", credentials=creds)


def fetch_sheet_data(spreadsheet_id: str, range_name: str = "A:Z") -> list[list[str]]:
    """Fetch data from a Google Sheet."""
    service = get_gsheets_service()
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
    return result.get("values", [])


LIST_FILE = Path.home() / ".config" / "nemlig" / "grocery_list.json"


def load_grocery_list() -> dict:
    """Load grocery list from config file."""
    if LIST_FILE.exists():
        return json.loads(LIST_FILE.read_text())
    return {"budget": 500.0, "items": []}


def save_grocery_list(data: dict) -> None:
    """Save grocery list to config file."""
    LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIST_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# Fridge inventory storage
FRIDGE_FILE = Path.home() / ".config" / "nemlig" / "fridge_inventory.json"


def load_fridge_inventory() -> dict:
    """Load fridge inventory from config file."""
    if FRIDGE_FILE.exists():
        return json.loads(FRIDGE_FILE.read_text())
    return {"items": [], "last_scan": None}


def save_fridge_inventory(data: dict) -> None:
    """Save fridge inventory to config file."""
    FRIDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FRIDGE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# Common produce items for YOLO detection mapping
PRODUCE_LABELS = {
    "apple": "æble",
    "banana": "banan",
    "orange": "appelsin",
    "lemon": "citron",
    "lime": "lime",
    "grape": "vindrue",
    "strawberry": "jordbær",
    "blueberry": "blåbær",
    "raspberry": "hindbær",
    "watermelon": "vandmelon",
    "pineapple": "ananas",
    "mango": "mango",
    "avocado": "avocado",
    "tomato": "tomat",
    "potato": "kartoffel",
    "carrot": "gulerod",
    "onion": "løg",
    "garlic": "hvidløg",
    "pepper": "peberfrugt",
    "cucumber": "agurk",
    "lettuce": "salat",
    "cabbage": "kål",
    "broccoli": "broccoli",
    "cauliflower": "blomkål",
    "spinach": "spinat",
    "mushroom": "champignon",
    "corn": "majs",
    "peas": "ærter",
    "beans": "bønner",
    "zucchini": "squash",
    "eggplant": "aubergine",
    "celery": "selleri",
    "asparagus": "asparges",
    "ginger": "ingefær",
    "parsley": "persille",
    "basil": "basilikum",
    "mint": "mynte",
    "cilantro": "koriander",
}


BASE_URL = "https://www.nemlig.com"
SEARCH_API_URL = "https://webapi.prod.knl.nemlig.it/searchgateway/api"


@dataclass
class AuthTokens:
    """Authentication tokens for Nemlig API."""
    xsrf_token: str
    bearer_token: str
    session: requests.Session

    def refresh(self) -> None:
        """Refresh bearer and XSRF tokens using the existing session."""
        headers = get_common_headers()
        headers["X-Correlation-Id"] = str(uuid.uuid4())
        resp = self.session.get(f"{BASE_URL}/webapi/Token", headers=headers)
        resp.raise_for_status()
        self.bearer_token = resp.json()["access_token"]

        resp = self.session.get(f"{BASE_URL}/webapi/AntiForgery", headers=headers)
        resp.raise_for_status()
        self.xsrf_token = resp.json()["Value"]


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

    spinner = Spinner("Connecting to nemlig.com")
    spinner.start()

    # Step 1: Get XSRF token
    resp = session.get(f"{BASE_URL}/webapi/AntiForgery", headers=headers)
    resp.raise_for_status()
    xsrf_data = resp.json()
    xsrf_token = xsrf_data["Value"]

    # Step 2: Get Bearer token
    headers["X-Correlation-Id"] = str(uuid.uuid4())
    resp = session.get(f"{BASE_URL}/webapi/Token", headers=headers)
    resp.raise_for_status()
    token_data = resp.json()
    bearer_token = token_data["access_token"]

    # Step 3: Login
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

    spinner.stop("Connected!")

    return AuthTokens(xsrf_token=xsrf_token, bearer_token=bearer_token, session=session)


def get_app_settings(auth: AuthTokens) -> dict:
    """Get app settings including timestamps needed for search."""
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token

    resp = auth.session.get(f"{BASE_URL}/webapi/v2/AppSettings/Website", headers=headers)

    # Auto-refresh tokens on 401
    if resp.status_code == 401:
        auth.refresh()
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

    # Auto-refresh tokens on 401
    if resp.status_code == 401:
        auth.refresh()
        headers["Authorization"] = f"Bearer {auth.bearer_token}"
        headers["X-XSRF-TOKEN"] = auth.xsrf_token
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

    # Auto-refresh tokens on 401
    if resp.status_code == 401:
        auth.refresh()
        headers["Authorization"] = f"Bearer {auth.bearer_token}"
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


# ── Nutrition / Macros ────────────────────────────────────────

def _parse_eu_number(s: str):
    """Parse the first number in a string, accepting comma decimals (e.g. '1,5 g')."""
    m = re.search(r"(\d+(?:[,.]\d+)?)", s or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _parse_kcal(s: str):
    """Extract kcal from values like '276 kJ / 65 kcal' or '65 kcal'."""
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*kcal", s or "", re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return _parse_eu_number(s)


def _parse_declaration_label(label_html: str) -> dict:
    """Parse the HTML nutrition table from a product's DeclarationLabel.

    Real Nemlig products carry nutrition as an HTML <table> inside
    DeclarationLabel (e.g. row "Protein" / "3.6 g"). The structured
    Declarations.NutritionFacts array shown in the API docs is rare in
    practice, so this is the primary path.
    """
    if not label_html:
        return {}
    rows = re.findall(
        r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
        label_html, re.IGNORECASE | re.DOTALL,
    )
    out: dict = {}
    for raw_name, raw_val in rows:
        name = re.sub(r"<[^>]+>", "", raw_name).strip().lower()
        # Decode common HTML entities so "m&#230;ttede" / "&aelig;" → "mættede" etc.
        name = (name.replace("&#230;", "æ").replace("&aelig;", "æ")
                    .replace("&#248;", "ø").replace("&oslash;", "ø")
                    .replace("&#229;", "å").replace("&aring;", "å"))
        val = re.sub(r"<[^>]+>", "", raw_val).strip()
        # Skip "heraf X" sub-rows (saturated fat, sugars) — only top-level macros
        if name.startswith("heraf"):
            continue
        if "energi" in name or "energy" in name:
            kc = _parse_kcal(val)
            if kc is not None and "kcal" not in out:
                out["kcal"] = kc
        elif "protein" in name and "protein_g" not in out:
            n = _parse_eu_number(val)
            if n is not None:
                out["protein_g"] = n
        elif ("fedt" in name or "fat" in name) and "fat_g" not in out:
            n = _parse_eu_number(val)
            if n is not None:
                out["fat_g"] = n
        elif ("kulhydrat" in name or "carb" in name) and "carbs_g" not in out:
            n = _parse_eu_number(val)
            if n is not None:
                out["carbs_g"] = n
    return out


def extract_nutrition(product: dict) -> dict | None:
    """Return per-100g {kcal, protein_g, fat_g, carbs_g} for a product detail.

    Tries the HTML DeclarationLabel first (where real data lives), then falls
    back to the structured NutritionFacts array if present. Returns None when
    no nutrition data is available.
    """
    out = _parse_declaration_label(product.get("DeclarationLabel") or "")

    # Fallback: structured NutritionFacts array (rare but documented)
    facts = (product.get("Declarations") or {}).get("NutritionFacts") or []
    for f in facts:
        name = (f.get("Name") or "").lower()
        val = f.get("Value") or ""
        if "energi" in name and "kcal" not in out:
            kc = _parse_kcal(val)
            if kc is not None:
                out["kcal"] = kc
        elif "protein" in name and "protein_g" not in out:
            n = _parse_eu_number(val)
            if n is not None:
                out["protein_g"] = n
        elif ("fedt" in name or "fat" in name) and "fat_g" not in out:
            n = _parse_eu_number(val)
            if n is not None:
                out["fat_g"] = n
        elif ("kulhydrat" in name or "carb" in name) and "carbs_g" not in out:
            n = _parse_eu_number(val)
            if n is not None:
                out["carbs_g"] = n
    return out or None


def format_nutrition(nutrition: dict | None) -> str:
    """Render a nutrition dict as a readable block. Empty string if no data."""
    if not nutrition:
        return ""
    lines = ["Nutrition (per 100g):"]
    if "kcal" in nutrition:
        lines.append(f"  Energy:   {nutrition['kcal']:.0f} kcal")
    if "protein_g" in nutrition:
        lines.append(f"  Protein:  {nutrition['protein_g']:.1f} g")
    if "carbs_g" in nutrition:
        lines.append(f"  Carbs:    {nutrition['carbs_g']:.1f} g")
    if "fat_g" in nutrition:
        lines.append(f"  Fat:      {nutrition['fat_g']:.1f} g")
    return "\n".join(lines)


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


def format_list_item(item: dict) -> str:
    """Format a grocery list item for display."""
    name = item.get("name", "Unknown")
    brand = item.get("brand", "")
    quantity = item.get("quantity", 1)
    unit_price = item.get("unit_price", 0)
    product_id = item.get("product_id", "")
    subtotal = unit_price * quantity

    brand_str = f" ({brand})" if brand else ""
    return f"  [{product_id}] {name}{brand_str} x{quantity} @ {unit_price:.2f} kr = {subtotal:.2f} kr"


CART_ART = r"""
   __________
  /         /|
 /_________/ |
 |  NEMLIG | |
 |_________|/
    O   O
"""

def format_list_summary(items: list, budget: float) -> str:
    """Format full grocery list with budget status."""
    lines = []

    if not items:
        lines.append("Your grocery list is empty.")
        lines.append(f"\nBudget: {budget:.2f} kr")
        lines.append("\nUse 'list add \"product\"' to add items")
        return "\n".join(lines)

    # Calculate total
    total = sum(item.get("unit_price", 0) * item.get("quantity", 1) for item in items)
    remaining = budget - total

    lines.append(f"Grocery List ({len(items)} items):\n")

    for item in items:
        lines.append(format_list_item(item))

    lines.append(f"\n  Subtotal:   {total:.2f} kr")
    lines.append(f"  Budget:     {budget:.2f} kr")
    lines.append(f"  Remaining:  {remaining:.2f} kr")

    # Budget bar visualization
    if budget > 0:
        bar_width = 30
        pct = (total / budget) * 100
        filled = min(int((total / budget) * bar_width), bar_width)
        empty = bar_width - filled

        # Color coding: green (<70%), yellow (70-90%), red (>90%)
        if pct > 100:
            bar = "\033[91m" + "█" * bar_width + "\033[0m"  # Red, overfilled
            status = "\033[91mOVER BUDGET!\033[0m"
        elif pct > 90:
            bar = "\033[91m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"  # Red
            status = f"\033[91m{pct:.0f}%\033[0m"
        elif pct > 70:
            bar = "\033[93m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"  # Yellow
            status = f"\033[93m{pct:.0f}%\033[0m"
        else:
            bar = "\033[92m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"  # Green
            status = f"\033[92m{pct:.0f}%\033[0m"

        lines.append(f"\n  [{bar}] {status}")

    return "\n".join(lines)


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

    # Nutrition / Macros
    nutrition = extract_nutrition(product)
    if nutrition:
        lines.append("")
        lines.append(format_nutrition(nutrition))

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


def _fetch_product_by_url(auth: "AuthTokens", product_url: str) -> dict:
    """Fetch a product detail by its URL slug (no redundant search call)."""
    page_settings = get_page_settings(auth)
    headers = get_common_headers()
    headers["Authorization"] = f"Bearer {auth.bearer_token}"
    headers["X-XSRF-TOKEN"] = auth.xsrf_token
    params = {"GetAsJson": "1", "t": page_settings["timeslotUtc"], "d": "1"}
    resp = auth.session.get(f"{BASE_URL}/{product_url}", headers=headers, params=params)
    resp.raise_for_status()
    for item in resp.json().get("content", []):
        if item.get("TemplateName") == "productdetailspot":
            return item
    return {}


def cmd_macros(auth: "AuthTokens", args: argparse.Namespace) -> int:
    """Search products and print a per-100g macros table for the top results."""
    query = args.query
    limit = max(1, min(args.limit, 10))

    print(f"Fetching macros for '{query}' (top {limit})...", file=sys.stderr)
    products = search_products(auth, query, limit)
    if not products:
        print(f"No products found for '{query}'")
        return 1

    print(f"\nMacros (per 100g) for '{query}':\n")
    print(f"  {'ID':<10} {'Product':<38} {'Price':>8}  {'kcal':>5} {'P':>5} {'C':>5} {'F':>5}")
    print("  " + "-" * 86)

    def _fmt(n, d):
        v = n.get(d)
        if isinstance(v, (int, float)):
            return f"{v:.0f}" if d == "kcal" else f"{v:.1f}"
        return "—"

    for p in products:
        pid = p.get("Id", "")
        name = (p.get("Name") or "")[:36]
        price = p.get("Price", 0)
        url = p.get("Url", "")
        try:
            detail = _fetch_product_by_url(auth, url) if url else {}
            n = extract_nutrition(detail) or {}
        except Exception:
            n = {}
        print(f"  {pid:<10} {name:<38} {price:>7.2f}   "
              f"{_fmt(n,'kcal'):>5} {_fmt(n,'protein_g'):>5} "
              f"{_fmt(n,'carbs_g'):>5} {_fmt(n,'fat_g'):>5}")
    print()
    print("  Use `just product <ID>` for the full nutrition label.")
    return 0


def search_recipes(auth: "AuthTokens", query: str, count: int = 5) -> list:
    """Hit the search API with recipeCount > 0 and return only the Recipes array."""
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
        "take": 0,
        "skip": 0,
        "recipeCount": count,
        "timestamp": page_settings["timestamp"],
        "timeslotUtc": page_settings["timeslotUtc"],
        "deliveryZoneId": page_settings["deliveryZoneId"],
    }
    if page_settings.get("userId"):
        params["includeFavorites"] = page_settings["userId"]

    resp = auth.session.get(f"{SEARCH_API_URL}/search", headers=headers, params=params)
    if resp.status_code == 401:
        auth.refresh()
        headers["Authorization"] = f"Bearer {auth.bearer_token}"
        resp = auth.session.get(f"{SEARCH_API_URL}/search", headers=headers, params=params)
    resp.raise_for_status()
    return resp.json().get("Recipes") or []


def format_recipe(r: dict) -> str:
    """Render a recipe dict as a 2-line block."""
    name = r.get("Name", "?")
    extras = []
    time = r.get("TotalTime")
    if time:
        extras.append(time)
    persons = r.get("NumberOfPersons")
    if isinstance(persons, int) and persons > 0:
        extras.append(f"{persons} {'person' if persons == 1 else 'personer'}")
    suffix = f"  ({', '.join(extras)})" if extras else ""
    url = r.get("Url", "")
    if url and not url.startswith("http"):
        url = BASE_URL + url
    line2 = f"    {url}" if url else ""
    return f"  • {name}{suffix}\n{line2}" if line2 else f"  • {name}{suffix}"


def cmd_recipes(auth: "AuthTokens", args: argparse.Namespace) -> int:
    """Search nemlig.com recipes for a term and print suggestions."""
    query = args.query
    limit = max(1, min(args.limit, 20))

    print(f"Searching recipes for '{query}'...", file=sys.stderr)
    recipes = search_recipes(auth, query, count=limit)
    if not recipes:
        print(f"No recipes found for '{query}'.")
        return 1

    print(f"\nFound {len(recipes)} recipes for '{query}':\n")
    for r in recipes:
        print(format_recipe(r))
        print()
    return 0


def cmd_list_recipes(auth: "AuthTokens", args: argparse.Namespace) -> int:
    """Suggest recipes that use items currently in the grocery list."""
    data = load_grocery_list()
    items = data.get("items", [])
    if not items:
        print("Your grocery list is empty. Add items first with `just list-add` or `just plan`.")
        return 1

    limit = max(1, min(args.limit, 20))
    per_item = 3
    print(f"Looking up recipes for {len(items)} list items...", file=sys.stderr)

    by_url: dict[str, dict] = {}
    for item in items:
        name = item.get("name", "").strip()
        if not name:
            continue
        try:
            recipes = search_recipes(auth, name, count=per_item)
        except Exception:
            continue
        for r in recipes:
            url = r.get("Url")
            if not url:
                continue
            entry = by_url.setdefault(url, {"recipe": r, "matches": set()})
            entry["matches"].add(name)

    if not by_url:
        print("No matching recipes found from items in your list.")
        return 0

    matches = sorted(by_url.values(), key=lambda x: -len(x["matches"]))[:limit]
    print(f"\nTop {len(matches)} recipes using items from your list:\n")
    for entry in matches:
        print(format_recipe(entry["recipe"]))
        print(f"    Uses: {', '.join(sorted(entry['matches']))}")
        print()
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


def cmd_list_show(args: argparse.Namespace) -> int:
    """Display the current grocery list."""
    data = load_grocery_list()
    print(format_list_summary(data["items"], data["budget"]))
    return 0


def cmd_list_add(auth: AuthTokens, args: argparse.Namespace) -> int:
    """Add a product to the grocery list."""
    product_id = args.product_id
    quantity = args.quantity

    # If not a numeric ID, treat as search query
    if not product_id.isdigit():
        print(f"Searching for '{product_id}'...", file=sys.stderr)
        products = search_products(auth, product_id, limit=10)

        if not products:
            print(f"No products found for '{product_id}'")
            return 1

        print(f"\nFound {len(products)} products:\n")
        for i, p in enumerate(products, 1):
            price = p.get("Price", 0)
            name = p.get("Name", "Unknown")
            brand = p.get("Brand", "")
            pid = p.get("Id", "")
            available = p.get("Availability", {}).get("IsAvailableInStock", False)
            stock = "In stock" if available else "OUT OF STOCK"
            print(f"  [{i}] {name} ({brand}) - {price:.2f} kr [{stock}]")

        print()
        try:
            choice = input("Enter number to add (or 'q' to cancel): ").strip()
            if choice.lower() == 'q':
                print("Cancelled.")
                return 0
            idx = int(choice) - 1
            if idx < 0 or idx >= len(products):
                print("Invalid selection.", file=sys.stderr)
                return 1
            product = products[idx]
            product_id = product.get("Id")
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 0
    else:
        print(f"Fetching product {product_id}...", file=sys.stderr)
        try:
            product = get_product_details(auth, product_id)
        except ProductNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    data = load_grocery_list()

    # Check if product already in list
    for item in data["items"]:
        if item["product_id"] == product_id:
            item["quantity"] += quantity
            item["unit_price"] = product.get("Price", item["unit_price"])
            save_grocery_list(data)
            print(f"Updated quantity: {item['name']} x{item['quantity']}")
            print(format_list_summary(data["items"], data["budget"]))
            return 0

    # Add new item
    new_item = {
        "product_id": product_id,
        "name": product.get("Name", "Unknown"),
        "brand": product.get("Brand", ""),
        "quantity": quantity,
        "unit_price": product.get("Price", 0),
    }
    data["items"].append(new_item)
    save_grocery_list(data)

    print(f"Added: {new_item['name']} x{quantity}")
    print()
    print(format_list_summary(data["items"], data["budget"]))
    return 0


def cmd_list_remove(args: argparse.Namespace) -> int:
    """Remove a product from the grocery list."""
    product_id = args.product_id

    data = load_grocery_list()

    for i, item in enumerate(data["items"]):
        if item["product_id"] == product_id:
            removed = data["items"].pop(i)
            save_grocery_list(data)
            print(f"Removed: {removed['name']}")
            print()
            print(format_list_summary(data["items"], data["budget"]))
            return 0

    print(f"Product {product_id} not found in list.", file=sys.stderr)
    return 1


def cmd_list_clear(args: argparse.Namespace) -> int:
    """Clear all items from the grocery list."""
    data = load_grocery_list()
    count = len(data["items"])
    data["items"] = []
    save_grocery_list(data)
    print(f"Cleared {count} items from list.")
    return 0


def cmd_list_budget(args: argparse.Namespace) -> int:
    """Show or set the budget."""
    data = load_grocery_list()

    if args.amount is not None:
        data["budget"] = args.amount
        save_grocery_list(data)
        print(f"Budget set to {args.amount:.2f} kr")
    else:
        budget = data["budget"]
        total = sum(item.get("unit_price", 0) * item.get("quantity", 1) for item in data["items"])
        remaining = budget - total
        print(f"Current budget: {budget:.2f} kr")
        print(f"List total:     {total:.2f} kr")
        print(f"Remaining:      {remaining:.2f} kr")

        # Progress bar
        if budget > 0:
            bar_width = 30
            pct = (total / budget) * 100
            filled = min(int((total / budget) * bar_width), bar_width)
            empty = bar_width - filled

            if pct > 100:
                bar = "\033[91m" + "█" * bar_width + "\033[0m"
                status = "\033[91mOVER BUDGET!\033[0m"
            elif pct > 90:
                bar = "\033[91m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"
                status = f"\033[91m{pct:.0f}%\033[0m"
            elif pct > 70:
                bar = "\033[93m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"
                status = f"\033[93m{pct:.0f}%\033[0m"
            else:
                bar = "\033[92m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"
                status = f"\033[92m{pct:.0f}%\033[0m"

            print(f"\n[{bar}] {status}")

    return 0


def cmd_list_sync(auth: AuthTokens, args: argparse.Namespace) -> int:
    """Sync grocery list to nemlig basket."""
    data = load_grocery_list()

    if not data["items"]:
        print("Grocery list is empty. Nothing to sync.")
        return 0

    print(f"Syncing {len(data['items'])} items to basket...", file=sys.stderr)

    success_count = 0
    for item in data["items"]:
        product_id = item["product_id"]
        quantity = item["quantity"]
        try:
            add_to_basket(auth, product_id, quantity)
            print(f"  ✓ {item['name']} x{quantity}")
            success_count += 1
        except Exception as e:
            print(f"  ✗ {item['name']} - Error: {e}", file=sys.stderr)

    print(f"\nSynced {success_count}/{len(data['items'])} items to basket.")

    if success_count > 0:
        print("\nUse 'basket' command to view your nemlig basket.")

    return 0 if success_count == len(data["items"]) else 1


# ============================================================================
# AI Meal Planning
# ============================================================================

MEAL_PLAN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search for grocery products on nemlig.com. Returns products with IDs, names, prices, and availability. If no results are found or the right product isn't in the results, try: (1) a different/simpler Danish search term, or (2) increase the limit to get more results (up to 50).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search term in Danish (e.g., 'mælk', 'hakket oksekød', 'pasta')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return. Start with 10, increase to 25 or 50 if the product you need isn't found.",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compare_macros",
            "description": "Fetch per-100g nutrition (kcal, protein, carbs, fat) for a list of product IDs and rank by protein-per-krone. Use this for PROTEIN ingredients (meat, fish, eggs, tofu, dairy) to pick the most macro-efficient option. Pass 2-5 candidate IDs from search_products results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of product IDs to compare (max 5)"
                    }
                },
                "required": ["product_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_grocery_list",
            "description": "Add a product to the grocery list by its product ID. Use search_products first to find the product ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "The product ID from search results"
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Number of items to add (default: 1)",
                        "default": 1
                    }
                },
                "required": ["product_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "view_grocery_list",
            "description": "View the current grocery list with all items, quantities, and budget status.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_grocery_list",
            "description": "Remove a product from the grocery list by its product ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "The product ID to remove"
                    }
                },
                "required": ["product_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_budget",
            "description": "Set the budget limit for the grocery list in Danish kroner (kr).",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "Budget amount in kr"
                    }
                },
                "required": ["amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clear_grocery_list",
            "description": "Clear all items from the grocery list. Use with caution.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]


def _budget_bar(total: float, budget: float) -> str:
    """Render a budget progress bar for the terminal."""
    pct = (total / budget * 100) if budget > 0 else 0
    bar_width = 20
    filled = int(bar_width * min(pct, 100) / 100)
    empty = bar_width - filled
    if pct > 100:
        color = "\033[91m"  # red
    elif pct > 80:
        color = "\033[93m"  # yellow
    else:
        color = "\033[92m"  # green
    reset = "\033[0m"
    bar = f"{color}{'█' * filled}{'░' * empty}{reset}"
    return f"  [{bar}] {total:.0f} / {budget:.0f} kr ({pct:.0f}%)"


def execute_meal_plan_tool(auth: AuthTokens, tool_name: str, tool_input: dict) -> str:
    """Execute a meal planning tool and return the result as a string."""
    try:
        if tool_name == "search_products":
            query = tool_input["query"]
            limit = tool_input.get("limit", 10)
            products = search_products(auth, query, limit=limit)

            if not products:
                return f"No products found for '{query}'. Try a simpler/different Danish search term, or increase the limit."

            results = []
            for p in products:
                pid = p.get("Id", "")
                name = p.get("Name", "Unknown")
                brand = p.get("Brand", "")
                price = p.get("Price", 0)
                unit_price = p.get("UnitPrice", "")
                available = p.get("Availability", {}).get("IsAvailableInStock", False)
                stock = "In stock" if available else "OUT OF STOCK"

                results.append(
                    f"- ID: {pid} | {name} ({brand}) | {price:.2f} kr | {unit_price} | {stock}"
                )

            header = f"Found {len(products)} products for '{query}':\n"
            hint = ""
            if len(products) == limit:
                hint = f"\n(Showing {limit} results — increase limit to see more)"
            return header + "\n".join(results) + hint

        elif tool_name == "compare_macros":
            ids = [str(p) for p in tool_input.get("product_ids", [])][:5]
            if not ids:
                return "Error: provide 1-5 product_ids."
            rows = []
            for pid in ids:
                try:
                    detail = get_product_details(auth, pid)
                except ProductNotFoundError as e:
                    rows.append({"id": pid, "error": str(e)})
                    continue
                nut = extract_nutrition(detail) or {}
                price = detail.get("Price", 0) or 0
                protein = nut.get("protein_g")
                pkr = (protein / price) if (protein and price) else None
                rows.append({
                    "id": pid,
                    "name": detail.get("Name", ""),
                    "price": price,
                    "kcal": nut.get("kcal"),
                    "protein_g": protein,
                    "carbs_g": nut.get("carbs_g"),
                    "fat_g": nut.get("fat_g"),
                    "protein_per_kr": round(pkr, 3) if pkr else None,
                })
            ranked = sorted(
                [r for r in rows if r.get("protein_per_kr") is not None],
                key=lambda r: r["protein_per_kr"], reverse=True,
            )
            no_data = [r for r in rows if "error" in r or r.get("protein_per_kr") is None]
            def _fmt(v, digits=1):
                return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "?"
            lines = ["Macros per 100g (ranked by protein-per-krone, best first):"]
            for r in ranked:
                lines.append(
                    f"- [{r['id']}] {r['name']} | {r['price']:.2f} kr | "
                    f"{_fmt(r['kcal'], 0)} kcal · "
                    f"P {_fmt(r['protein_g'])}g · C {_fmt(r['carbs_g'])}g · F {_fmt(r['fat_g'])}g | "
                    f"protein/kr: {r['protein_per_kr']}"
                )
            for r in no_data:
                if "error" in r:
                    lines.append(f"- [{r['id']}] (no data: {r['error']})")
                else:
                    lines.append(f"- [{r['id']}] {r.get('name','')} | {r.get('price',0):.2f} kr | (no nutrition data)")
            if ranked:
                lines.append(f"\nRecommendation: prefer ID {ranked[0]['id']} for highest protein density.")
            return "\n".join(lines)

        elif tool_name == "add_to_grocery_list":
            product_id = str(tool_input["product_id"])
            quantity = tool_input.get("quantity", 1)

            # Fetch product details
            try:
                product = get_product_details(auth, product_id)
            except ProductNotFoundError as e:
                return f"Error: {e}"

            data = load_grocery_list()

            # Check if already in list
            for item in data["items"]:
                if str(item["product_id"]) == product_id:
                    item["quantity"] += quantity
                    save_grocery_list(data)
                    total = sum(i["unit_price"] * i["quantity"] for i in data["items"])
                    print(f"  \033[92m  + {item['name']} x{item['quantity']}\033[0m")
                    print(_budget_bar(total, data["budget"]))
                    return f"Updated quantity: {item['name']} x{item['quantity']} (was x{item['quantity'] - quantity})"

            # Add new item
            new_item = {
                "product_id": product_id,
                "name": product.get("Name", "Unknown"),
                "brand": product.get("Brand", ""),
                "unit_price": product.get("Price", 0),
                "quantity": quantity,
            }
            data["items"].append(new_item)
            save_grocery_list(data)

            total = sum(i["unit_price"] * i["quantity"] for i in data["items"])
            print(f"  \033[92m  + {new_item['name']} x{quantity} ({new_item['unit_price']:.2f} kr)\033[0m")
            print(_budget_bar(total, data["budget"]))
            return f"Added: {new_item['name']} x{quantity} ({new_item['unit_price']:.2f} kr each)\nList total: {total:.2f} kr / Budget: {data['budget']:.2f} kr"

        elif tool_name == "view_grocery_list":
            data = load_grocery_list()
            if not data["items"]:
                return f"Grocery list is empty. Budget: {data['budget']:.2f} kr"

            lines = [f"Grocery List ({len(data['items'])} items):"]
            total = 0
            for item in data["items"]:
                subtotal = item["unit_price"] * item["quantity"]
                total += subtotal
                lines.append(f"- [{item['product_id']}] {item['name']} x{item['quantity']} = {subtotal:.2f} kr")

            lines.append(f"\nSubtotal: {total:.2f} kr")
            lines.append(f"Budget: {data['budget']:.2f} kr")
            lines.append(f"Remaining: {data['budget'] - total:.2f} kr")

            pct = (total / data['budget'] * 100) if data['budget'] > 0 else 0
            lines.append(f"Budget used: {pct:.0f}%")

            return "\n".join(lines)

        elif tool_name == "remove_from_grocery_list":
            product_id = str(tool_input["product_id"])
            data = load_grocery_list()

            for i, item in enumerate(data["items"]):
                if str(item["product_id"]) == product_id:
                    removed = data["items"].pop(i)
                    save_grocery_list(data)
                    total = sum(i["unit_price"] * i["quantity"] for i in data["items"])
                    print(f"  \033[91m  - {removed['name']}\033[0m")
                    print(_budget_bar(total, data["budget"]))
                    return f"Removed: {removed['name']} x{removed['quantity']}"

            return f"Product {product_id} not found in grocery list"

        elif tool_name == "set_budget":
            amount = float(tool_input["amount"])
            data = load_grocery_list()
            data["budget"] = amount
            save_grocery_list(data)
            total = sum(i["unit_price"] * i["quantity"] for i in data["items"])
            print(f"  \033[93m  Budget set to {amount:.0f} kr\033[0m")
            print(_budget_bar(total, amount))
            return f"Budget set to {amount:.2f} kr"

        elif tool_name == "clear_grocery_list":
            data = load_grocery_list()
            count = len(data["items"])
            data["items"] = []
            save_grocery_list(data)
            return f"Cleared {count} items from grocery list"

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        return f"Error executing {tool_name}: {e}"


def _format_markdown(text: str) -> str:
    """Convert markdown to ANSI-styled terminal output."""
    lines = text.split("\n")
    result = []
    bold = "\033[1m"
    dim = "\033[2m"
    cyan = "\033[96m"
    reset = "\033[0m"
    for line in lines:
        # Headers: ### / ## / #
        stripped = line.lstrip()
        if stripped.startswith("### "):
            result.append(f"  {bold}{stripped[4:]}{reset}")
        elif stripped.startswith("## "):
            result.append(f"  {bold}{cyan}{stripped[3:]}{reset}")
        elif stripped.startswith("# "):
            result.append(f"  {bold}{cyan}{stripped[2:]}{reset}")
        else:
            # Bold: **text**
            formatted = re.sub(r'\*\*(.+?)\*\*', rf'{bold}\1{reset}', line)
            # Italic: *text* (but not inside bold)
            formatted = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', rf'{dim}\1{reset}', formatted)
            # Bullet points
            if formatted.lstrip().startswith("- "):
                indent = len(formatted) - len(formatted.lstrip())
                content = formatted.lstrip()[2:]
                formatted = " " * indent + f"  {dim}•{reset} {content}"
            result.append(formatted)
    return "\n".join(result)


_DIET_OPTIONS = [
    "No restrictions",
    "Vegetarian",
    "Vegan",
    "Pescatarian",
    "Keto / low-carb",
]

_ALLERGY_OPTIONS = [
    "Lactose",
    "Gluten",
    "Nuts",
    "Shellfish",
    "Eggs",
    "Soy",
]

_ORGANIC_OPTIONS = [
    "Always organic",
    "Prefer organic",
    "No preference",
]

_COOKING_OPTIONS = [
    "Quick (under 20 min)",
    "Medium (under 45 min)",
    "No limit",
]

_CUISINE_OPTIONS = [
    "Mixed / no preference",
    "Mediterranean",
    "Nordic",
    "Asian",
    "Mexican / Latin",
]

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MEALS = ["Breakfast", "Lunch", "Dinner"]


# Sentinel returned by survey helpers when the user wants to go back one step.
_BACK = object()


def _is_back(raw: str) -> bool:
    return raw.strip().lower() in ("b", "back")


def _pick_one(label: str, options: list[str], default: int = 1, allow_back: bool = True):
    """Print numbered options, return the selected string (or _BACK)."""
    print(f"  {label}")
    for i, opt in enumerate(options, 1):
        print(f"    [{i}] {opt}")
    hint = "  Choice (" + str(default) + (", b=back" if allow_back else "") + "): "
    raw = input(hint).strip()
    if allow_back and _is_back(raw):
        return _BACK
    idx = int(raw) if raw.isdigit() and 1 <= int(raw) <= len(options) else default
    chosen = options[idx - 1]
    print(f"  \u2192 {chosen}\n")
    return chosen


def _pick_many(label: str, options: list[str], allow_back: bool = True):
    """Print numbered options, allow comma-separated multi-select, return list (or _BACK)."""
    print(f"  {label}")
    for i, opt in enumerate(options, 1):
        print(f"    [{i}] {opt}")
    hint = "  Choices (comma-separated, Enter=none" + (", b=back" if allow_back else "") + "): "
    raw = input(hint).strip()
    if allow_back and _is_back(raw):
        return _BACK
    if not raw:
        print("  \u2192 None\n")
        return []
    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(options):
            selected.append(options[int(part) - 1])
    if selected:
        print(f"  \u2192 {', '.join(selected)}\n")
    else:
        print("  \u2192 None\n")
    return selected


def _meal_grid_inner(stdscr, initial=None):
    """Curses inner function: interactive 7x3 meal toggle grid.

    Returns a schedule dict, or _BACK if the user pressed 'b'.
    `initial` is an optional schedule dict to pre-fill the grid.
    """
    # State: 7 days x 3 meals, all checked by default (or pre-filled from initial)
    if initial:
        grid = [[m in initial.get(day, []) for m in _MEALS] for day in _DAYS]
    else:
        grid = [[True] * len(_MEALS) for _ in range(len(_DAYS))]
    cursor_row, cursor_col = 0, 0

    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)    # checked
    curses.init_pair(2, curses.COLOR_WHITE, -1)    # unchecked
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)  # cursor
    curses.init_pair(4, curses.COLOR_CYAN, -1)     # title
    curses.init_pair(5, curses.COLOR_YELLOW, -1)   # shortcuts

    DAY_W = 14
    MEAL_W = 12
    TITLE_ROW = 1
    HELP_ROW = 2
    COL_HDR_ROW = 4
    GRID_ROW = 5
    FOOTER_ROW = GRID_ROW + len(_DAYS) + 1
    warn_msg = ""

    while True:
        stdscr.erase()

        # Title
        stdscr.addstr(TITLE_ROW, 2, "Weekly Meal Schedule",
                      curses.color_pair(4) | curses.A_BOLD)

        # Help
        stdscr.addstr(HELP_ROW, 2,
                      "Arrows: move  |  Space: toggle cell  |  d: toggle day  |  c: toggle column  |  Enter: confirm  |  b: back",
                      curses.A_DIM)

        # Column headers
        x_off = DAY_W + 2
        for j, meal in enumerate(_MEALS):
            stdscr.addstr(COL_HDR_ROW, x_off + j * MEAL_W,
                          meal.center(MEAL_W),
                          curses.A_BOLD | curses.A_UNDERLINE)

        # Grid rows
        total_meals = 0
        active_days = 0
        count_x = x_off + len(_MEALS) * MEAL_W + 2
        for i, day in enumerate(_DAYS):
            y = GRID_ROW + i
            row_count = sum(grid[i])
            total_meals += row_count
            if row_count > 0:
                active_days += 1
            day_attr = curses.A_BOLD if i == cursor_row else 0
            stdscr.addstr(y, 2, day.ljust(DAY_W), day_attr)

            for j in range(len(_MEALS)):
                x = x_off + j * MEAL_W
                checked = grid[i][j]
                symbol = " \u2713 " if checked else " \u00b7 "

                if i == cursor_row and j == cursor_col:
                    attr = curses.color_pair(3) | curses.A_BOLD
                elif checked:
                    attr = curses.color_pair(1) | curses.A_BOLD
                else:
                    attr = curses.color_pair(2) | curses.A_DIM

                stdscr.addstr(y, x, symbol.center(MEAL_W), attr)

            # Per-row count, e.g. "(2/3)" — green if any selected, dim otherwise
            count_str = f"({row_count}/{len(_MEALS)})"
            count_attr = (curses.color_pair(1) | curses.A_BOLD) if row_count > 0 else curses.A_DIM
            stdscr.addstr(y, count_x, count_str, count_attr)

        # Live total summary
        summary_attr = (curses.color_pair(1) | curses.A_BOLD) if total_meals > 0 \
                        else (curses.color_pair(5) | curses.A_BOLD)
        summary = f"\u2192 {active_days} {'day' if active_days == 1 else 'days'} \u00b7 {total_meals} {'meal' if total_meals == 1 else 'meals'} total"
        stdscr.addstr(FOOTER_ROW, 2, summary, summary_attr)

        # Shortcuts
        stdscr.addstr(FOOTER_ROW + 2, 2,
                      "[d] Day  [c] Column  [a] All  [n] None  [w] Weekday dinners  [f] Full weekends  [b] Back",
                      curses.color_pair(5))

        # Warning (if any)
        if warn_msg:
            stdscr.addstr(FOOTER_ROW + 4, 2, warn_msg,
                          curses.color_pair(1) | curses.A_BOLD)

        stdscr.refresh()
        key = stdscr.getch()
        warn_msg = ""

        if key == curses.KEY_UP:
            cursor_row = (cursor_row - 1) % len(_DAYS)
        elif key == curses.KEY_DOWN:
            cursor_row = (cursor_row + 1) % len(_DAYS)
        elif key == curses.KEY_LEFT:
            cursor_col = (cursor_col - 1) % len(_MEALS)
        elif key == curses.KEY_RIGHT:
            cursor_col = (cursor_col + 1) % len(_MEALS)
        elif key == ord(" "):
            grid[cursor_row][cursor_col] = not grid[cursor_row][cursor_col]
        elif key in (curses.KEY_ENTER, 10, 13):
            # Validate: at least one meal selected
            if not any(grid[i][j] for i in range(len(_DAYS)) for j in range(len(_MEALS))):
                warn_msg = "Select at least one meal!"
                continue
            break
        elif key in (ord("b"), ord("B")):
            return _BACK
        elif key == ord("a"):
            for i in range(len(_DAYS)):
                for j in range(len(_MEALS)):
                    grid[i][j] = True
        elif key == ord("n"):
            for i in range(len(_DAYS)):
                for j in range(len(_MEALS)):
                    grid[i][j] = False
        elif key == ord("w"):
            for i in range(5):  # Mon-Fri
                grid[i][0] = False  # Breakfast off
                grid[i][1] = False  # Lunch off
                grid[i][2] = True   # Dinner on
        elif key == ord("f"):
            for j in range(len(_MEALS)):  # Sat + Sun all on
                grid[5][j] = True
                grid[6][j] = True
        elif key in (ord("d"), ord("D")):
            # Toggle the whole day (current row): off if any selected, else all on
            row_has_any = any(grid[cursor_row])
            for j in range(len(_MEALS)):
                grid[cursor_row][j] = not row_has_any
        elif key in (ord("c"), ord("C")):
            # Toggle the whole meal column (e.g. all dinners across the week)
            col_has_any = any(grid[i][cursor_col] for i in range(len(_DAYS)))
            for i in range(len(_DAYS)):
                grid[i][cursor_col] = not col_has_any

    # Build result — omit days with no meals
    schedule: dict[str, list[str]] = {}
    for i, day in enumerate(_DAYS):
        selected = [_MEALS[j] for j in range(len(_MEALS)) if grid[i][j]]
        if selected:
            schedule[day] = selected
    return schedule


def _meal_grid_picker(initial=None):
    """Show an interactive curses grid for selecting meals per day.

    Returns a schedule dict (day → list of meal names), or _BACK if the
    user pressed 'b'. Falls back to all meals on all days if not running
    in a terminal.
    """
    if not sys.stdout.isatty():
        return {day: list(_MEALS) for day in _DAYS}
    return curses.wrapper(_meal_grid_inner, initial)


def _step_diet(state, idx, total):
    print(_step_header(idx, total, "Diet"))
    return _pick_one("What kind of diet?", _DIET_OPTIONS, allow_back=idx > 1)


def _step_allergies(state, idx, total):
    print(_step_header(idx, total, "Allergies"))
    selected = _pick_many("Allergies / intolerances", _ALLERGY_OPTIONS)
    if selected is _BACK:
        return _BACK
    raw = input("  Other allergies? (Enter=none, b=back): ").strip()
    if _is_back(raw):
        return _BACK
    if raw:
        selected.append(raw)
        print(f"  \u2192 Added: {raw}\n")
    else:
        print()
    return selected


def _step_people(state, idx, total):
    print(_step_header(idx, total, "Number of people"))
    raw = input("  How many people are you cooking for? (1, b=back): ").strip()
    if _is_back(raw):
        return _BACK
    people = int(raw) if raw.isdigit() and int(raw) > 0 else 1
    print(f"  \u2192 {people} {'person' if people == 1 else 'people'}\n")
    return people


def _step_schedule(state, idx, total):
    print(_step_header(idx, total, "Weekly meal schedule"))
    print("  Pick which meals you want planned each day.")
    print("  (arrows to move · space: toggle cell · d: toggle day · c: toggle column · enter: confirm · b: back)\n")
    schedule = _meal_grid_picker(initial=state.get("schedule"))
    if schedule is _BACK:
        return _BACK
    total_meals = sum(len(m) for m in schedule.values())
    active_days = len(schedule)
    print(f"  \u2192 {active_days} {'day' if active_days == 1 else 'days'} \u00b7 "
          f"{total_meals} {'meal' if total_meals == 1 else 'meals'}/week\n")
    return schedule


def _step_organic(state, idx, total):
    print(_step_header(idx, total, "Organic preference"))
    return _pick_one("How important is organic?", _ORGANIC_OPTIONS, default=3)


def _step_cooking(state, idx, total):
    print(_step_header(idx, total, "Cooking time"))
    return _pick_one("How long can you spend per meal?", _COOKING_OPTIONS, default=2)


def _step_cuisine(state, idx, total):
    print(_step_header(idx, total, "Cuisine style"))
    return _pick_one("Preferred cuisine?", _CUISINE_OPTIONS)


def _step_budget(state, idx, total):
    print(_step_header(idx, total, "Budget"))
    raw = input("  Weekly budget in kr? (500, b=back): ").strip()
    if _is_back(raw):
        return _BACK
    try:
        budget = float(raw) if raw else 500.0
    except ValueError:
        budget = 500.0
    print(f"  \u2192 {budget:.0f} kr\n")
    return budget


def _step_extra(state, idx, total):
    print(_step_header(idx, total, "Anything else?"))
    raw = input("  Extra notes (Enter=skip, b=back):\n  : ").strip()
    if _is_back(raw):
        return _BACK
    if raw:
        print(f"  \u2192 {raw}\n")
    else:
        print()
    return raw


def _step_header(idx, total, title):
    return f"  \u2500\u2500 Step {idx}/{total}: {title} \u2500\u2500"


_SURVEY_STEPS = [
    ("diet", _step_diet),
    ("allergies", _step_allergies),
    ("people", _step_people),
    ("schedule", _step_schedule),
    ("organic", _step_organic),
    ("cooking", _step_cooking),
    ("cuisine", _step_cuisine),
    ("budget", _step_budget),
    ("extra", _step_extra),
]


def _meal_plan_survey() -> dict:
    """Run an interactive survey to collect meal planning preferences.

    Type `b` (or `back`) at any prompt to revisit the previous step. In the
    meal-grid step, press the `b` key.

    Returns a dict with keys matching _SURVEY_STEPS. Raises
    EOFError/KeyboardInterrupt if the user cancels.
    """
    print("\n  🍽️  Meal Planner — Setup")
    print("  ─────────────────────────────────────")
    print("  Type 'b' at any prompt to go back to the previous question.\n")

    state: dict = {}
    total = len(_SURVEY_STEPS)
    i = 0
    while i < total:
        key, fn = _SURVEY_STEPS[i]
        result = fn(state, i + 1, total)
        if result is _BACK:
            if i == 0:
                print("  (already at the first question)\n")
                continue
            i -= 1
            continue
        state[key] = result
        i += 1

    return state


def _format_survey_message(survey: dict) -> str:
    """Format survey dict into a structured first message for the LLM."""
    allergy_str = ", ".join(survey["allergies"]) if survey["allergies"] else "None"
    lines = [
        "MEAL PLAN REQUEST",
        f"- Diet: {survey['diet']}",
        f"- Allergies/restrictions: {allergy_str}",
        f"- People: {survey['people']}",
        "- Schedule:",
    ]
    for day in _DAYS:
        meals = survey["schedule"].get(day, [])
        if meals:
            lines.append(f"    {day}: {', '.join(meals)}")
    lines.append(f"- Organic: {survey['organic']}")
    lines.append(f"- Cooking time: {survey['cooking']}")
    lines.append(f"- Cuisine: {survey['cuisine']}")
    lines.append(f"- Budget: {survey['budget']:.0f} kr")
    if survey.get("extra"):
        lines.append(f"- Extra notes: {survey['extra']}")
    if MEAL_TEMPLATE and survey.get("use_template", True):
        lines.append("")
        lines.append(render_meal_template_block(MEAL_TEMPLATE))
    return "\n".join(lines)


def _format_survey_message_with_template(survey: dict, use_template: bool) -> str:
    """Wrapper that respects an explicit per-call use_template override."""
    survey = dict(survey)
    survey["use_template"] = use_template
    return _format_survey_message(survey)


def cmd_show_template() -> int:
    """Print the current diet template (or a friendly hint if missing)."""
    if not MEAL_TEMPLATE:
        print(f"\n  No template found at {MEAL_TEMPLATE_PATH}")
        print("  Create meal_template.json or run with --no-template to disable.\n")
        return 1
    print()
    print(f"  📐 {MEAL_TEMPLATE.get('name', 'unnamed')}")
    print(f"     Source: {MEAL_TEMPLATE_PATH}")
    print()
    for line in render_meal_template_block(MEAL_TEMPLATE).split("\n"):
        print(f"  {line}")
    print()
    return 0


def _tool_progress_message(tool_name: str, tool_input: dict) -> str:
    """Return a user-friendly progress line for a tool call."""
    if tool_name == "search_products":
        q = tool_input.get("query", "")
        return f"  \033[96m🔍\033[0m Searching \"{q}\"..."
    elif tool_name == "add_to_grocery_list":
        qty = tool_input.get("quantity", 1)
        pid = tool_input.get("product_id", "?")
        return f"  \033[92m🛒\033[0m Adding product {pid} x{qty}..."
    elif tool_name == "remove_from_grocery_list":
        pid = tool_input.get("product_id", "?")
        return f"  \033[91m🗑️\033[0m  Removing product {pid}..."
    elif tool_name == "view_grocery_list":
        return "  \033[96m📋\033[0m Checking list..."
    elif tool_name == "set_budget":
        amt = tool_input.get("amount", "?")
        return f"  \033[93m💰\033[0m Setting budget to {amt} kr"
    elif tool_name == "clear_grocery_list":
        return "  \033[91m🗑️\033[0m  Clearing grocery list..."
    return f"  \033[90m[{tool_name}]\033[0m"


MEAL_PLAN_SYSTEM_PROMPT = """You are a grocery meal planner for nemlig.com (a Danish online grocery store). Always respond in English.

You follow a strict 3-step flow:

STEP 1 — UNDERSTAND
The user's first message contains structured survey data with their diet, restrictions, number of people, weekly meal schedule (which specific meals on which days), organic preference, cooking time limit, cuisine style, budget, and extra notes. Parse it carefully. Do NOT ask follow-up questions — all requirements are provided. Scale all quantities to match the number of people. Briefly confirm the requirements (one sentence) and immediately move to Step 2.

STEP 2 — SEARCH & BUILD
Based on the user's requirements, decide on meals matching the user's weekly schedule (only plan the specific meals listed for each day). Then IMMEDIATELY:
- Use search_products to find each ingredient on nemlig.com (search in Danish: "kyllingebryst", "hakket oksekød", "pasta", "ris", "æg", etc.)
- For PROTEIN ingredients (meat, fish, eggs, tofu, dairy with high protein like skyr/cottage cheese): pick 2-4 reasonable candidates from the search results and call compare_macros with their IDs. The tool returns per-100g macros and ranks by protein-per-krone — add the top-ranked product unless something else (organic, brand, package size) clearly outweighs density.
- For non-protein ingredients (vegetables, grains, condiments, fruits): just pick the best-priced available product, no need for compare_macros.
- Use add_to_grocery_list to add them
- Do this for ALL ingredients before responding
- Do NOT list meals or recipes in text yet — just silently search and add everything

STEP 3 — RECEIPT & APPROVAL
Once all items are added, present a single clean summary with:

1. MEAL PLAN — list each day with the specific meals from the schedule (only include days and meal types the user selected)
2. GROCERY RECEIPT — list every item, quantity, and price like a receipt:
     Havregryn (finvalsede) x1         12.95 kr
     Skyr naturel x2                   49.90 kr
     Bananer x6                        18.00 kr
     ...
     ─────────────────────────────────
     TOTAL                            488.24 kr
     BUDGET                           500.00 kr
     REMAINING                         11.76 kr

3. Ask: "Approve this plan? (yes/no)" — wait for the user to confirm.

If the user says no or wants changes:
- NEVER use clear_grocery_list. Keep the existing list intact.
- Only remove specific items with remove_from_grocery_list and add new ones with add_to_grocery_list.
- Then use view_grocery_list and show an updated receipt.
If the user says yes, respond with a short confirmation that their list is ready to sync.

IMPORTANT RULES:
- NEVER use clear_grocery_list after the initial plan is built. Only add/remove individual items when adjusting.
- Never ask clarifying questions in Step 1. Just start planning and shopping.
- Always search nemlig.com BEFORE suggesting meals — only suggest what's actually available.
- Product names on nemlig.com are in Danish, so always search in Danish.
- Stay within budget unless the user explicitly says it's ok to go over.
- Use view_grocery_list to check current state before presenting the receipt.
- Respect the organic preference: if "Always organic", only pick organic products. If "Prefer organic", pick organic when available at a similar price.
- Respect cooking time limits: if "Quick (under 20 min)", only suggest meals that are fast to prepare.
- Follow the cuisine style preference when choosing meals."""


def meal_plan_chat(auth: AuthTokens, cli: bool = False, use_template: bool = True) -> int:
    """Run the AI meal planning chat interface.

    When *cli* is False (default), an interactive survey collects the
    user's requirements first and auto-sends them to the LLM.  When True,
    the original free-text chat is used instead.

    When *use_template* is True (default) and meal_template.json exists,
    the diet template (macros, priority foods, rules) is appended to the
    first user message so every plan respects it.
    """
    template_active = bool(MEAL_TEMPLATE) and use_template
    if template_active:
        print(f"\n  🎯 Diet template active: {MEAL_TEMPLATE.get('name', 'unnamed')}")
        m = MEAL_TEMPLATE.get("daily_macros", {})
        if m:
            print(f"     Targets/day: {m.get('calories', '?')} kcal · "
                  f"{m.get('protein_g', '?')}g P · "
                  f"{m.get('carbs_g', '?')}g C · "
                  f"{m.get('fat_g', '?')}g F")
        print("     (use --no-template to disable)\n")
    if not OPENAI_AVAILABLE and not ANTHROPIC_AVAILABLE:
        print("\n  Error: No AI SDK installed.")
        print("  Run: uv add openai   (or: uv add anthropic)")
        return 1

    ai_result = get_ai_client()
    if not ai_result:
        print("\n  Error: AI provider not configured.")
        print("  Set AI_PROVIDER and credentials in ~/.config/nemlig/login.json or as environment variables.")
        print(f"  Supported providers: azure, anthropic, {', '.join(_PROVIDER_REGISTRY)}, custom")
        return 1

    client, model = ai_result
    messages = [{"role": "system", "content": MEAL_PLAN_SYSTEM_PROMPT}]

    # Clear grocery list for fresh planning session
    data = load_grocery_list()
    data["items"] = []

    # ── Survey or CLI chat welcome ──────────────────────────
    first_message: str | None = None

    if cli:
        # Original free-text flow
        save_grocery_list(data)
        print("\n  🍽️  AI Meal Planner")
        print("  ─────────────────────────────────────────────────────")
        print("  Tell me what you want to eat this week and I'll")
        print("  build your meal plan and shopping list automatically.")
        print()
        print("  Just describe your preferences:")
        print("    'Healthy and filling, I cycle a lot'")
        print("    'Easy vegetarian meals under 400 kr'")
        print("    'High protein, minimal cooking'")
        print()
        print(f"  Budget: {data['budget']:.0f} kr")
        print("  ─────────────────────────────────────────────────────\n")
    else:
        # Guided survey
        try:
            survey = _meal_plan_survey()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Exiting meal planner.\n")
            return 0

        data["budget"] = survey["budget"]
        save_grocery_list(data)

        first_message = _format_survey_message_with_template(survey, template_active)

        # Show summary
        total_meals = sum(len(m) for m in survey["schedule"].values())
        active_days = len(survey["schedule"])
        print("  ─────────────────────────────────────────────────────")
        print(f"  ✓ {survey['diet']} | {survey['people']} people | "
              f"{active_days} days, {total_meals} meals/week")
        print(f"  ✓ Budget: {survey['budget']:.0f} kr")
        print(f"  ✓ {survey['organic']} | {survey['cooking']} | {survey['cuisine']}")
        if survey["allergies"]:
            print(f"  ✓ Avoiding: {', '.join(survey['allergies'])}")
        if survey.get("extra"):
            print(f"  ✓ {survey['extra']}")
        print("  ─────────────────────────────────────────────────────")
        print("\n  Starting meal planning...\n")

    # ── Helper to run one LLM round + tool-call loop ────────────
    def _run_turn(user_content: str) -> bool:
        """Send *user_content*, execute tool calls, print response.

        Returns True on success, False on error.
        """
        messages.append({"role": "user", "content": user_content})

        spinner = Spinner("Planning meals")
        spinner.start()

        try:
            response = client.chat.completions.create(
                model=model,
                max_completion_tokens=16384,
                tools=MEAL_PLAN_TOOLS,
                messages=messages
            )
        except Exception as e:
            spinner.stop("Error")
            print(f"\n  Error: {e}\n")
            messages.pop()
            return False

        spinner.stop("")

        choice = response.choices[0]
        while choice.finish_reason == "tool_calls":
            assistant_msg = choice.message
            messages.append(assistant_msg)

            for tool_call in assistant_msg.tool_calls:
                tool_name = tool_call.function.name
                tool_input = json.loads(tool_call.function.arguments)

                # Friendly progress line
                print(_tool_progress_message(tool_name, tool_input))

                result = execute_meal_plan_tool(auth, tool_name, tool_input)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result
                })

            spinner = Spinner("Finding ingredients")
            spinner.start()
            try:
                response = client.chat.completions.create(
                    model=model,
                    max_completion_tokens=16384,
                    tools=MEAL_PLAN_TOOLS,
                    messages=messages
                )
            except Exception as e:
                spinner.stop("Error")
                print(f"\n  Error: {e}\n")
                return False
            spinner.stop("")
            choice = response.choices[0]

        if choice.message.content:
            text = choice.message.content
            formatted = _format_markdown(text)
            indented = "\n".join(f"  {line}" for line in formatted.split("\n"))
            print(f"\n  \033[96m🤖\033[0m {indented.lstrip()}\n")
            messages.append({"role": "assistant", "content": text})

        return True

    # ── Auto-send survey as first message ───────────────────────
    if first_message is not None:
        if not _run_turn(first_message):
            return 1

    # ── Chat loop for follow-up adjustments ─────────────────────
    is_first_free_text = cli and first_message is None
    while True:
        try:
            user_input = input("  you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Exiting meal planner.\n")
            return 0

        if not user_input:
            continue

        if user_input.lower() in ("done", "exit", "quit", "q"):
            print("\n  Exiting meal planner.\n")
            return 0

        if is_first_free_text and template_active:
            user_input = user_input + "\n\n" + render_meal_template_block(MEAL_TEMPLATE)
            is_first_free_text = False

        _run_turn(user_input)

    return 0


# ============================================================================
# Google Form / Recipe Import
# ============================================================================

RECIPE_EXTRACT_PROMPT = """You are a grocery shopping assistant. Given a list of meals or recipes that someone wants to make, extract ALL the ingredients needed.

For each ingredient:
1. Identify the ingredient name in Danish (translate if needed for nemlig.com)
2. Estimate the quantity needed
3. Prioritize by importance (essential ingredients first, optional garnishes last)

Output your response as a JSON array of ingredients:
[
  {"ingredient": "hakket oksekød", "quantity": 500, "unit": "g", "priority": 1, "for_recipe": "Spaghetti Bolognese"},
  {"ingredient": "spaghetti", "quantity": 500, "unit": "g", "priority": 1, "for_recipe": "Spaghetti Bolognese"},
  ...
]

Be thorough - include all ingredients mentioned in recipes. Use Danish names for products when possible as this is for a Danish grocery store."""


def extract_ingredients_from_recipes(recipes_text: str) -> list[dict]:
    """Use AI to extract ingredients from recipe text."""
    if not OPENAI_AVAILABLE and not ANTHROPIC_AVAILABLE:
        raise RuntimeError("No AI SDK installed (need openai or anthropic)")

    ai_result = get_ai_client()
    if not ai_result:
        raise RuntimeError("AI provider not configured")

    client, model = ai_result

    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=4096,
        messages=[
            {"role": "system", "content": RECIPE_EXTRACT_PROMPT},
            {
                "role": "user",
                "content": f"Extract all ingredients from these meal plans/recipes:\n\n{recipes_text}\n\nRespond with ONLY a JSON array, no other text."
            }
        ],
    )

    # Parse JSON from response
    response_text = response.choices[0].message.content.strip()

    # Handle markdown code blocks
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    return json.loads(response_text)


def process_form_recipes(auth: AuthTokens, spreadsheet_id: str | None = None) -> int:
    """Fetch recipes from Google Form responses and build grocery list."""
    print("\n  📋 Recipe Import from Google Form")
    print("  ─────────────────────────────────────────────────────\n")

    # Get spreadsheet ID
    config = load_gsheets_config()
    if spreadsheet_id:
        config["spreadsheet_id"] = spreadsheet_id
        save_gsheets_config(config)
    elif not config.get("spreadsheet_id"):
        print("  No spreadsheet ID configured.")
        print("  Usage: nemlig_cli.py import <SPREADSHEET_ID>")
        print("  Or: nemlig_cli.py import --setup")
        return 1

    sheet_id = config.get("spreadsheet_id", spreadsheet_id)

    # Fetch data from sheet
    print(f"  Fetching data from Google Sheet...")
    spinner = Spinner("Connecting to Google Sheets")
    spinner.start()

    try:
        rows = fetch_sheet_data(sheet_id)
    except FileNotFoundError as e:
        spinner.stop("Error")
        print(f"\n  {e}")
        return 1
    except Exception as e:
        spinner.stop("Error")
        print(f"\n  Error fetching sheet: {e}")
        return 1

    spinner.stop(f"Found {len(rows)} rows")

    if not rows:
        print("  No data in spreadsheet.")
        return 1

    # Assume first row is headers
    headers = rows[0] if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []

    print(f"  Headers: {headers}")
    print(f"  Data rows: {len(data_rows)}\n")

    if not data_rows:
        print("  No form responses yet.")
        return 0

    # Combine all recipe text from form responses
    recipes_text = ""
    for i, row in enumerate(data_rows, 1):
        # Skip timestamp column (usually first), combine rest
        recipe_data = " | ".join(row[1:]) if len(row) > 1 else row[0] if row else ""
        if recipe_data.strip():
            recipes_text += f"\nSubmission {i}:\n{recipe_data}\n"

    if not recipes_text.strip():
        print("  No recipe data found in form responses.")
        return 0

    print("  Form responses:")
    print("  " + "-" * 50)
    for line in recipes_text.strip().split("\n"):
        print(f"  {line}")
    print("  " + "-" * 50)

    # Extract ingredients using LLM
    print("\n  Analyzing recipes with AI...")
    spinner = Spinner("Extracting ingredients")
    spinner.start()

    try:
        ingredients = extract_ingredients_from_recipes(recipes_text)
    except json.JSONDecodeError as e:
        spinner.stop("Error parsing AI response")
        print(f"\n  Could not parse ingredients: {e}")
        return 1
    except Exception as e:
        spinner.stop("Error")
        print(f"\n  Error extracting ingredients: {e}")
        return 1

    spinner.stop(f"Found {len(ingredients)} ingredients")

    if not ingredients:
        print("  No ingredients extracted.")
        return 0

    # Show extracted ingredients
    print("\n  Extracted ingredients:")
    print("  " + "-" * 50)
    for ing in ingredients:
        qty = ing.get("quantity", "")
        unit = ing.get("unit", "")
        name = ing.get("ingredient", "")
        recipe = ing.get("for_recipe", "")
        print(f"  • {name} ({qty}{unit}) - for {recipe}")
    print("  " + "-" * 50)

    # Confirm with user
    print(f"\n  Ready to search and add {len(ingredients)} ingredients to your grocery list.")
    try:
        confirm = input("  Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return 0

    if confirm and confirm != "y":
        print("  Cancelled.")
        return 0

    # Search and add each ingredient
    print("\n  Adding ingredients to grocery list...\n")
    added = 0
    failed = []

    for ing in ingredients:
        name = ing.get("ingredient", "")
        qty = ing.get("quantity", 1)

        # Determine quantity to add (default to 1 item)
        add_qty = 1
        if isinstance(qty, (int, float)) and qty > 0:
            # Rough conversion: if unit is 'g' or 'ml' and qty > 100, still add 1 package
            add_qty = 1

        spinner = Spinner(f"Searching: {name}")
        spinner.start()

        try:
            products = search_products(auth, name, limit=3)

            if not products:
                spinner.stop(f"✗ Not found: {name}")
                failed.append(name)
                continue

            # Pick first available product
            product = None
            for p in products:
                if p.get("Availability", {}).get("IsAvailableInStock", False):
                    product = p
                    break

            if not product:
                product = products[0]  # Use first even if out of stock

            product_id = str(product.get("Id"))
            product_name = product.get("Name", name)
            price = product.get("Price", 0)

            # Add to grocery list
            data = load_grocery_list()

            # Check if already in list
            existing = None
            for item in data["items"]:
                if str(item["product_id"]) == product_id:
                    existing = item
                    break

            if existing:
                existing["quantity"] += add_qty
                spinner.stop(f"✓ Updated: {product_name} (now x{existing['quantity']})")
            else:
                new_item = {
                    "product_id": product_id,
                    "name": product_name,
                    "brand": product.get("Brand", ""),
                    "unit_price": price,
                    "quantity": add_qty,
                }
                data["items"].append(new_item)
                spinner.stop(f"✓ Added: {product_name} - {price:.2f} kr")

            save_grocery_list(data)
            added += 1

        except Exception as e:
            spinner.stop(f"✗ Error: {name} - {e}")
            failed.append(name)

    # Summary
    print("\n  " + "=" * 50)
    print(f"  ✓ Added {added} items to grocery list")
    if failed:
        print(f"  ✗ Failed to find: {', '.join(failed)}")

    # Show list summary
    data = load_grocery_list()
    total = sum(i["unit_price"] * i["quantity"] for i in data["items"])
    print(f"\n  List total: {total:.2f} kr / Budget: {data['budget']:.2f} kr")

    if total > data["budget"]:
        print(f"  \033[91m⚠ Over budget by {total - data['budget']:.2f} kr!\033[0m")

    print("\n  Use 'list' to view full grocery list, 'list sync' to push to nemlig.\n")

    return 0


def cmd_import_setup() -> int:
    """Interactive setup for Google Sheets import."""
    print("\n  📋 Google Sheets Import Setup")
    print("  ─────────────────────────────────────────────────────\n")

    print("  Step 1: Google Cloud Setup")
    print("  ─────────────────────────────────────────────────────")
    print("  1. Go to https://console.cloud.google.com/")
    print("  2. Create a new project or select existing")
    print("  3. Enable 'Google Sheets API'")
    print("  4. Go to APIs & Services > Credentials")
    print("  5. Create OAuth 2.0 Client ID (Desktop app)")
    print("  6. Download the credentials JSON file")
    print(f"  7. Save it as: {GSHEETS_CREDENTIALS_FILE}")
    print()

    print("  Step 2: Spreadsheet ID")
    print("  ─────────────────────────────────────────────────────")
    print("  Your Google Form responses are saved to a linked spreadsheet.")
    print("  The spreadsheet ID is in the URL:")
    print("  https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit")
    print()

    try:
        sheet_id = input("  Enter spreadsheet ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return 0

    if sheet_id:
        config = load_gsheets_config()
        config["spreadsheet_id"] = sheet_id
        save_gsheets_config(config)
        print(f"\n  ✓ Spreadsheet ID saved to {GSHEETS_CONFIG_FILE}")
        print("  Run 'just import' to fetch and process recipes.")
    else:
        print("  No spreadsheet ID provided.")

    return 0


# ============================================================================
# Fridge Scanner (Raspberry Pi AI Camera)
# ============================================================================

def lookup_barcode(barcode: str) -> dict | None:
    """Look up product info from barcode using OpenFoodFacts."""
    if not SCANNER_AVAILABLE:
        return None

    try:
        api = openfoodfacts.API(user_agent="NemligCLI/1.0")
        product = api.product.get(barcode, fields=["code", "product_name", "brands", "quantity", "categories_tags"])

        if product and product.get("product_name"):
            return {
                "barcode": barcode,
                "name": product.get("product_name", "Unknown"),
                "brand": product.get("brands", ""),
                "quantity": product.get("quantity", ""),
                "categories": product.get("categories_tags", []),
                "source": "openfoodfacts"
            }
    except Exception:
        pass

    return None


def scan_barcodes_from_image(image) -> list[str]:
    """Detect and decode barcodes from an image using pyzbar."""
    if not SCANNER_AVAILABLE:
        return []

    # Convert to grayscale for better detection
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image

    # Decode barcodes
    barcodes = pyzbar.decode(gray)
    return [barcode.data.decode("utf-8") for barcode in barcodes]


def detect_produce_from_image(image, imx500=None) -> list[dict]:
    """Detect fruits/vegetables using YOLO on IMX500 or fallback to basic detection."""
    detected = []

    if PICAMERA_AVAILABLE and imx500:
        # Use IMX500 AI accelerator for object detection
        try:
            # Get detections from IMX500 (assumes YOLO model loaded)
            detections = imx500.get_outputs()
            if detections:
                for det in detections:
                    label = det.get("label", "").lower()
                    confidence = det.get("confidence", 0)

                    if confidence > 0.5 and label in PRODUCE_LABELS:
                        detected.append({
                            "name": PRODUCE_LABELS.get(label, label),
                            "name_en": label,
                            "confidence": confidence,
                            "source": "imx500_yolo"
                        })
        except Exception:
            pass

    # Fallback: Use color-based detection for common produce
    # This is a simplified approach - real implementation would use a proper model
    if not detected and SCANNER_AVAILABLE:
        try:
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

            # Detect yellow (banana, lemon)
            yellow_mask = cv2.inRange(hsv, (20, 100, 100), (35, 255, 255))
            if cv2.countNonZero(yellow_mask) > 5000:
                detected.append({"name": "banan/citron", "name_en": "banana/lemon", "confidence": 0.3, "source": "color"})

            # Detect orange
            orange_mask = cv2.inRange(hsv, (10, 100, 100), (20, 255, 255))
            if cv2.countNonZero(orange_mask) > 5000:
                detected.append({"name": "appelsin", "name_en": "orange", "confidence": 0.3, "source": "color"})

            # Detect red (apple, tomato, pepper)
            red_mask1 = cv2.inRange(hsv, (0, 100, 100), (10, 255, 255))
            red_mask2 = cv2.inRange(hsv, (160, 100, 100), (180, 255, 255))
            red_mask = cv2.bitwise_or(red_mask1, red_mask2)
            if cv2.countNonZero(red_mask) > 5000:
                detected.append({"name": "æble/tomat", "name_en": "apple/tomato", "confidence": 0.3, "source": "color"})

            # Detect green (cucumber, lettuce, broccoli)
            green_mask = cv2.inRange(hsv, (35, 50, 50), (85, 255, 255))
            if cv2.countNonZero(green_mask) > 5000:
                detected.append({"name": "grøntsag", "name_en": "vegetable", "confidence": 0.3, "source": "color"})

        except Exception:
            pass

    return detected


def run_fridge_scanner(auth: AuthTokens | None = None) -> int:
    """Run the fridge scanner to inventory items."""
    print("\n  📷 Fridge Scanner")
    print("  ─────────────────────────────────────────────────────")

    if not SCANNER_AVAILABLE:
        print("\n  Error: Scanner libraries not installed.")
        print("  Run: uv add pyzbar opencv-python Pillow openfoodfacts")
        return 1

    # Check for Raspberry Pi AI Camera
    imx500 = None
    picam2 = None
    use_webcam = True

    if PICAMERA_AVAILABLE:
        try:
            print("  Detected Raspberry Pi - initializing AI Camera...")
            picam2 = Picamera2()

            # Try to load YOLO model for produce detection
            try:
                imx500 = IMX500("/usr/share/imx500-models/imx500_network_yolov8n_pp.rpk")
                print("  ✓ YOLO model loaded on IMX500")
            except Exception:
                print("  ⚠ YOLO model not available, using basic detection")

            picam2.configure(picam2.create_preview_configuration())
            picam2.start()
            use_webcam = False
            print("  ✓ Raspberry Pi AI Camera ready")
        except Exception as e:
            print(f"  ⚠ Could not initialize Pi Camera: {e}")
            print("  Falling back to webcam...")
            use_webcam = True

    if use_webcam:
        try:
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                print("\n  Error: No camera available.")
                print("  Connect a webcam or run on Raspberry Pi with AI Camera.")
                return 1
            print("  ✓ Webcam ready")
        except Exception as e:
            print(f"\n  Error initializing camera: {e}")
            return 1

    print("\n  Instructions:")
    print("  - Point camera at items in your fridge")
    print("  - Barcodes will be scanned automatically")
    print("  - Fruits/vegetables will be detected by AI")
    print("  - Press 'a' to add detected item to inventory")
    print("  - Press 's' to suggest items to buy")
    print("  - Press 'q' to quit")
    print("  ─────────────────────────────────────────────────────\n")

    inventory = load_fridge_inventory()
    scanned_barcodes = set()
    detected_items = []
    last_detection_time = 0

    try:
        while True:
            # Capture frame
            if use_webcam:
                ret, frame = cap.read()
                if not ret:
                    continue
            else:
                frame = picam2.capture_array()

            current_time = time.time()

            # Scan barcodes
            barcodes = scan_barcodes_from_image(frame)
            for barcode in barcodes:
                if barcode not in scanned_barcodes:
                    scanned_barcodes.add(barcode)
                    print(f"  🔍 Barcode detected: {barcode}")

                    product = lookup_barcode(barcode)
                    if product:
                        print(f"     ✓ Found: {product['name']} ({product['brand']})")
                        detected_items.append(product)

                        # Auto-add to inventory
                        existing = next((i for i in inventory["items"] if i.get("barcode") == barcode), None)
                        if existing:
                            existing["count"] = existing.get("count", 1) + 1
                            print(f"     Updated count: {existing['count']}")
                        else:
                            inventory["items"].append({
                                "barcode": barcode,
                                "name": product["name"],
                                "brand": product.get("brand", ""),
                                "count": 1,
                                "added": time.strftime("%Y-%m-%d %H:%M"),
                                "source": "barcode"
                            })
                            print(f"     Added to inventory!")
                        save_fridge_inventory(inventory)
                    else:
                        print(f"     ⚠ Product not found in database")

            # Detect produce (throttle to every 2 seconds)
            if current_time - last_detection_time > 2:
                produce = detect_produce_from_image(frame, imx500)
                for item in produce:
                    if item["confidence"] > 0.5:
                        print(f"  🥬 Detected: {item['name']} (confidence: {item['confidence']:.0%})")
                last_detection_time = current_time

            # Show frame with overlays
            display = frame.copy()

            # Draw barcode boxes
            if SCANNER_AVAILABLE:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
                for barcode in pyzbar.decode(gray):
                    pts = barcode.polygon
                    if pts:
                        pts = [(p.x, p.y) for p in pts]
                        cv2.polylines(display, [cv2.convexHull(cv2.array(pts, dtype="int32").reshape((-1, 1, 2)))], True, (0, 255, 0), 2)

            # Show inventory count
            cv2.putText(display, f"Inventory: {len(inventory['items'])} items", (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(display, "Press 'q' to quit, 's' to suggest shopping", (10, 60),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            cv2.imshow("Fridge Scanner", display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('s'):
                # Suggest items to buy based on what's running low
                print("\n  📋 Suggested items to add to grocery list:")
                for item in inventory["items"]:
                    if item.get("count", 1) <= 1:
                        print(f"     - {item['name']} (running low)")
                print()

    except KeyboardInterrupt:
        print("\n  Stopped scanning.")
    finally:
        if use_webcam:
            cap.release()
        elif picam2:
            picam2.stop()
        cv2.destroyAllWindows()

    # Update last scan time
    inventory["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_fridge_inventory(inventory)

    print(f"\n  ✓ Inventory saved: {len(inventory['items'])} items")
    return 0


def cmd_fridge_show() -> int:
    """Show current fridge inventory."""
    inventory = load_fridge_inventory()

    print("\n  🧊 Fridge Inventory")
    print("  ─────────────────────────────────────────────────────")

    if not inventory["items"]:
        print("  Your fridge inventory is empty.")
        print("  Run 'scan' to start scanning items.")
        return 0

    print(f"  Last scan: {inventory.get('last_scan', 'Never')}")
    print(f"  Total items: {len(inventory['items'])}\n")

    for item in inventory["items"]:
        name = item.get("name", "Unknown")
        brand = item.get("brand", "")
        count = item.get("count", 1)
        added = item.get("added", "")
        source = item.get("source", "")

        brand_str = f" ({brand})" if brand else ""
        print(f"  • {name}{brand_str} x{count}")
        if added:
            print(f"    Added: {added} [{source}]")

    print("  ─────────────────────────────────────────────────────\n")
    return 0


def cmd_fridge_clear() -> int:
    """Clear fridge inventory."""
    inventory = load_fridge_inventory()
    count = len(inventory["items"])
    inventory["items"] = []
    save_fridge_inventory(inventory)
    print(f"  Cleared {count} items from fridge inventory.")
    return 0


def cmd_fridge_suggest(auth: AuthTokens) -> int:
    """Suggest grocery items based on fridge contents using AI."""
    if not OPENAI_AVAILABLE and not ANTHROPIC_AVAILABLE:
        print("  Error: No AI SDK installed (need openai or anthropic).")
        return 1

    ai_result = get_ai_client()
    if not ai_result:
        print("  Error: AI provider not configured.")
        return 1

    client, model = ai_result

    inventory = load_fridge_inventory()
    grocery_list = load_grocery_list()

    if not inventory["items"]:
        print("  Fridge inventory is empty. Run 'scan' first.")
        return 1

    # Build context
    fridge_items = ", ".join(item["name"] for item in inventory["items"])
    list_items = ", ".join(item["name"] for item in grocery_list["items"]) if grocery_list["items"] else "empty"

    print("\n  🤖 Analyzing fridge contents and suggesting items...\n")

    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=1024,
        messages=[
            {"role": "system", "content": "You are a helpful Danish grocery shopping assistant. Give practical, concise suggestions."},
            {
                "role": "user",
                "content": f"""Based on my fridge contents and current grocery list, suggest what I should buy.

Fridge contents: {fridge_items}
Current grocery list: {list_items}
Budget: {grocery_list['budget']:.2f} kr

Please suggest:
1. Items that would complement what I have (for complete meals)
2. Staples that might be running low
3. Fresh items that need regular replenishment

Keep suggestions practical for a Danish grocery store (nemlig.com).
Format as a simple bullet list with item names in Danish."""
            }
        ],
    )

    print("  Suggestions based on your fridge:")
    print("  ─────────────────────────────────────────────────────")
    text = response.choices[0].message.content
    if text:
        for line in text.split("\n"):
            print(f"  {line}")
    print("  ─────────────────────────────────────────────────────\n")

    return 0


def interactive_mode(auth: AuthTokens, username: str) -> int:
    """Run interactive REPL mode."""
    print_welcome(username)

    # Set up tab completion
    completer = NemligCompleter()
    readline.set_completer(completer.complete)
    readline.set_completer_delims(" ")

    # macOS uses libedit which needs different binding syntax
    if readline.__doc__ and "libedit" in readline.__doc__:
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    # Show quick help
    print("    Commands: search <query> | list | plan | basket | help | quit\n")

    while True:
        try:
            cmd = input("  nemlig> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n    Goodbye! 👋\n")
            return 0

        if not cmd:
            continue

        parts = cmd.split()
        command = parts[0].lower()

        if command in ("quit", "exit", "q"):
            print("\n    Goodbye! 👋\n")
            return 0

        elif command == "help":
            print("""
    Available commands:
    ─────────────────────────────────────────────────────
    search <query>      Search for products
    details <id>        Show product details
    list                Show your grocery list
    list add <query>    Add product to list (search by name)
    list remove <id>    Remove product from list
    list clear          Clear grocery list
    list budget [amt]   Show/set budget
    list sync           Push list to nemlig basket
    basket              Show nemlig basket
    plan                🤖 AI meal planner (interactive chat)
    import              📋 Import recipes from Google Form
    scan                📷 Scan fridge with camera
    fridge              🧊 View fridge inventory
    fridge suggest      🤖 AI suggestions based on fridge
    fridge clear        Clear fridge inventory
    help                Show this help
    quit                Exit
    ─────────────────────────────────────────────────────
""")

        elif command == "plan":
            meal_plan_chat(auth)

        elif command == "import":
            process_form_recipes(auth)

        elif command == "scan":
            run_fridge_scanner(auth)

        elif command == "fridge":
            if len(parts) > 1:
                subcmd = parts[1].lower()
                if subcmd == "clear":
                    cmd_fridge_clear()
                elif subcmd == "suggest":
                    cmd_fridge_suggest(auth)
                else:
                    cmd_fridge_show()
            else:
                cmd_fridge_show()

        elif command == "search" and len(parts) > 1:
            query = " ".join(parts[1:])
            spinner = Spinner(f"Searching for '{query}'")
            spinner.start()
            products = search_products(auth, query, limit=10)
            if not products:
                spinner.stop(f"No products found for '{query}'")
            else:
                spinner.stop(f"Found {len(products)} products")
                print()
                for p in products:
                    price = p.get("Price", 0)
                    name = p.get("Name", "Unknown")
                    brand = p.get("Brand", "")
                    pid = p.get("Id", "")
                    available = p.get("Availability", {}).get("IsAvailableInStock", False)
                    stock = "In stock" if available else "OUT OF STOCK"
                    print(f"  [{pid}] {name} ({brand}) - {price:.2f} kr [{stock}]")
            print()

        elif command == "details" and len(parts) > 1:
            product_id = parts[1]
            spinner = Spinner(f"Loading product {product_id}")
            spinner.start()
            try:
                product = get_product_details(auth, product_id)
                spinner.stop("Product loaded")
                print()
                print(format_product_details(product))
                print()
            except ProductNotFoundError as e:
                spinner.stop()
                print(f"Error: {e}\n")

        elif command == "list":
            if len(parts) == 1:
                # Show list
                data = load_grocery_list()
                print(format_list_summary(data["items"], data["budget"]))
                print()
            elif parts[1] == "add" and len(parts) > 2:
                query = " ".join(parts[2:])
                spinner = Spinner(f"Searching for '{query}'")
                spinner.start()
                products = search_products(auth, query, limit=10)
                if not products:
                    spinner.stop(f"No products found for '{query}'")
                else:
                    spinner.stop(f"Found {len(products)} products")
                    print()
                    for i, p in enumerate(products, 1):
                        price = p.get("Price", 0)
                        name = p.get("Name", "Unknown")
                        brand = p.get("Brand", "")
                        available = p.get("Availability", {}).get("IsAvailableInStock", False)
                        stock = "In stock" if available else "OUT OF STOCK"
                        print(f"  [{i}] {name} ({brand}) - {price:.2f} kr [{stock}]")
                    print()
                    try:
                        choice = input("  Enter number to add (or 'q' to cancel): ").strip()
                        if choice.lower() != 'q':
                            idx = int(choice) - 1
                            if 0 <= idx < len(products):
                                product = products[idx]
                                data = load_grocery_list()
                                product_id = product.get("Id")
                                # Check if already in list
                                found = False
                                for item in data["items"]:
                                    if item["product_id"] == product_id:
                                        item["quantity"] += 1
                                        item["unit_price"] = product.get("Price", item["unit_price"])
                                        found = True
                                        break
                                if not found:
                                    data["items"].append({
                                        "product_id": product_id,
                                        "name": product.get("Name", "Unknown"),
                                        "brand": product.get("Brand", ""),
                                        "quantity": 1,
                                        "unit_price": product.get("Price", 0),
                                    })
                                save_grocery_list(data)
                                print(f"\n  ✓ Added: {product.get('Name')}")
                                print(format_list_summary(data["items"], data["budget"]))
                    except (ValueError, KeyboardInterrupt):
                        print("Cancelled.")
                    print()
            elif parts[1] == "remove" and len(parts) > 2:
                product_id = parts[2]
                data = load_grocery_list()
                for i, item in enumerate(data["items"]):
                    if item["product_id"] == product_id:
                        removed = data["items"].pop(i)
                        save_grocery_list(data)
                        print(f"  ✓ Removed: {removed['name']}\n")
                        break
                else:
                    print(f"  Product {product_id} not in list\n")
            elif parts[1] == "clear":
                data = load_grocery_list()
                count = len(data["items"])
                data["items"] = []
                save_grocery_list(data)
                print(f"  ✓ Cleared {count} items\n")
            elif parts[1] == "budget":
                data = load_grocery_list()
                if len(parts) > 2:
                    try:
                        data["budget"] = float(parts[2])
                        save_grocery_list(data)
                        print(f"  ✓ Budget set to {data['budget']:.2f} kr\n")
                    except ValueError:
                        print("  Invalid amount\n")
                else:
                    total = sum(item.get("unit_price", 0) * item.get("quantity", 1) for item in data["items"])
                    budget = data["budget"]
                    remaining = budget - total
                    print(f"  Budget: {budget:.2f} kr | Used: {total:.2f} kr | Remaining: {remaining:.2f} kr")

                    # Progress bar
                    if budget > 0:
                        bar_width = 30
                        pct = (total / budget) * 100
                        filled = min(int((total / budget) * bar_width), bar_width)
                        empty = bar_width - filled

                        if pct > 100:
                            bar = "\033[91m" + "█" * bar_width + "\033[0m"
                            status = "\033[91mOVER BUDGET!\033[0m"
                        elif pct > 90:
                            bar = "\033[91m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"
                            status = f"\033[91m{pct:.0f}%\033[0m"
                        elif pct > 70:
                            bar = "\033[93m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"
                            status = f"\033[93m{pct:.0f}%\033[0m"
                        else:
                            bar = "\033[92m" + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"
                            status = f"\033[92m{pct:.0f}%\033[0m"

                        print(f"  [{bar}] {status}\n")
            elif parts[1] == "sync":
                data = load_grocery_list()
                if not data["items"]:
                    print("  List is empty\n")
                else:
                    print(f"  Syncing {len(data['items'])} items...")
                    for item in data["items"]:
                        try:
                            add_to_basket(auth, item["product_id"], item["quantity"])
                            print(f"    ✓ {item['name']} x{item['quantity']}")
                        except Exception as e:
                            print(f"    ✗ {item['name']} - {e}")
                    print("  Done! Use 'basket' to view.\n")
            else:
                print("  Usage: list | list add <query> | list remove <id> | list clear | list budget [amt] | list sync\n")

        elif command == "basket":
            spinner = Spinner("Loading basket")
            spinner.start()
            basket = get_basket(auth)
            spinner.stop("Basket loaded")
            lines = basket.get("Lines", [])
            if not lines:
                print("  Basket is empty\n")
            else:
                print(f"\n  Basket ({len(lines)} items):\n")
                total = 0
                for line in lines:
                    print(f"  {format_basket_line(line)}")
                    total += line.get("Price", 0)
                print(f"\n  Total: {total:.2f} kr\n")

        else:
            print("  Unknown command. Type 'help' for available commands.\n")


def main():
    parser = argparse.ArgumentParser(
        description=LOGO.format(version=VERSION),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Credentials:
  Credentials are loaded from ~/.config/nemlig/login.json if it exists.
  CLI options (-u, -p) override the config file values.

  Config file format:
    {"username": "email@example.com", "password": "secret"}

Examples:
  %(prog)s search "cocio"
  %(prog)s list add "mælk"
  %(prog)s list
  %(prog)s list sync
  %(prog)s basket
        """
    )

    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("-u", "--username", help="Nemlig.com email/username (overrides config file)")
    parser.add_argument("-p", "--password", help="Nemlig.com password (overrides config file)")

    subparsers = parser.add_subparsers(dest="command", required=False)

    # Search command
    search_parser = subparsers.add_parser("search", help="Search for products")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("-l", "--limit", type=int, default=10, help="Max results (default: 10)")

    # Details command
    details_parser = subparsers.add_parser("details", help="Show detailed product info")
    details_parser.add_argument("product_id", help="Product ID to view")

    # Macros command — search + nutrition table
    macros_parser = subparsers.add_parser("macros", help="🥩 Show per-100g macros for top search results")
    macros_parser.add_argument("query", help="Search query")
    macros_parser.add_argument("-l", "--limit", type=int, default=5, help="Max results, capped at 10 (default: 5)")

    # Recipes command — nemlig.com recipe suggestions for a search term
    recipes_parser = subparsers.add_parser("recipes", help="🍳 Show recipes from nemlig.com for a search term")
    recipes_parser.add_argument("query", help="Search query (ingredient, dish, etc.)")
    recipes_parser.add_argument("-l", "--limit", type=int, default=5, help="Max recipes (default: 5)")

    # List recipes command — recipes that use items from the grocery list
    list_recipes_parser = subparsers.add_parser("list-recipes", help="🍽️  Suggest recipes using items from your list")
    list_recipes_parser.add_argument("-l", "--limit", type=int, default=8, help="Max recipes (default: 8)")

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

    # List command with subcommands
    list_parser = subparsers.add_parser("list", help="Manage grocery list")
    list_sub = list_parser.add_subparsers(dest="list_cmd")

    # list (show) - default when no subcommand
    list_sub.add_parser("show", help="Show current grocery list")

    # list add
    list_add_parser = list_sub.add_parser("add", help="Add product to list")
    list_add_parser.add_argument("product_id", help="Product ID or search term")
    list_add_parser.add_argument("-q", "--quantity", type=int, default=1, help="Quantity (default: 1)")

    # list remove
    list_remove_parser = list_sub.add_parser("remove", help="Remove product from list")
    list_remove_parser.add_argument("product_id", help="Product ID to remove")

    # list clear
    list_sub.add_parser("clear", help="Clear all items from list")

    # list budget
    list_budget_parser = list_sub.add_parser("budget", help="Show or set budget")
    list_budget_parser.add_argument("amount", nargs="?", type=float, help="New budget amount in kr")

    # list sync
    list_sub.add_parser("sync", help="Push list items to nemlig basket")

    # Plan command (AI meal planning)
    plan_parser = subparsers.add_parser("plan", help="🤖 AI meal planner - build grocery list from recipes")
    plan_parser.add_argument("--cli", action="store_true",
                             help="Skip survey, use free-text chat instead")
    plan_parser.add_argument("--no-template", action="store_true",
                             help="Ignore meal_template.json for this run")

    # Show diet template
    subparsers.add_parser("template", help="📐 Show the active diet template (meal_template.json)")

    # Import command (Google Form recipes)
    import_parser = subparsers.add_parser("import", help="📋 Import recipes from Google Form/Sheet")
    import_parser.add_argument("spreadsheet_id", nargs="?", help="Google Spreadsheet ID (from URL)")
    import_parser.add_argument("--setup", action="store_true", help="Run interactive setup")

    # Scan command (fridge camera scanner)
    subparsers.add_parser("scan", help="📷 Scan fridge with camera (barcode + AI detection)")

    # Fridge command (inventory management)
    fridge_parser = subparsers.add_parser("fridge", help="🧊 Manage fridge inventory")
    fridge_sub = fridge_parser.add_subparsers(dest="fridge_cmd")
    fridge_sub.add_parser("show", help="Show fridge inventory (default)")
    fridge_sub.add_parser("clear", help="Clear fridge inventory")
    fridge_sub.add_parser("suggest", help="AI suggestions based on fridge contents")

    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    # Handle list commands that don't require authentication
    if args.command == "list":
        list_cmd = args.list_cmd
        # Commands that don't need auth
        if list_cmd is None or list_cmd == "show":
            return cmd_list_show(args)
        elif list_cmd == "remove":
            return cmd_list_remove(args)
        elif list_cmd == "clear":
            return cmd_list_clear(args)
        elif list_cmd == "budget":
            return cmd_list_budget(args)
        # Commands that need auth fall through to below

    # Handle import --setup (no auth needed)
    if args.command == "import" and args.setup:
        return cmd_import_setup()

    # Handle fridge commands that don't need auth
    if args.command == "fridge":
        fridge_cmd = args.fridge_cmd
        if fridge_cmd is None or fridge_cmd == "show":
            return cmd_fridge_show()
        elif fridge_cmd == "clear":
            return cmd_fridge_clear()
        # suggest needs auth, falls through

    # Diet template view (no auth needed)
    if args.command == "template":
        return cmd_show_template()

    # Load credentials: config file first, CLI overrides
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
        print(
            f"Error: Missing {' and '.join(missing)}. "
            f"Provide via config file or -u/-p options.",
            file=sys.stderr,
        )
        return 1

    try:
        # Authenticate
        auth = login(username, password)

        # Interactive mode if no command given
        if args.command is None:
            return interactive_mode(auth, username)

        # Single command mode - show welcome banner
        print_welcome(username)

        # Execute command
        if args.command == "search":
            return cmd_search(auth, args)
        elif args.command == "details":
            return cmd_details(auth, args)
        elif args.command == "macros":
            return cmd_macros(auth, args)
        elif args.command == "recipes":
            return cmd_recipes(auth, args)
        elif args.command == "list-recipes":
            return cmd_list_recipes(auth, args)
        elif args.command == "basket":
            return cmd_basket(auth, args)
        elif args.command == "add":
            return cmd_add(auth, args)
        elif args.command == "history":
            return cmd_history(auth, args)
        elif args.command == "list":
            # List commands that require auth
            if args.list_cmd == "add":
                return cmd_list_add(auth, args)
            elif args.list_cmd == "sync":
                return cmd_list_sync(auth, args)
        elif args.command == "plan":
            return meal_plan_chat(auth, cli=args.cli, use_template=not args.no_template)
        elif args.command == "import":
            return process_form_recipes(auth, args.spreadsheet_id)
        elif args.command == "scan":
            return run_fridge_scanner(auth)
        elif args.command == "fridge":
            # Only suggest needs auth (show/clear handled above)
            if args.fridge_cmd == "suggest":
                return cmd_fridge_suggest(auth)
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
