# Nemlig.com CLI

Command-line interface for [nemlig.com](https://www.nemlig.com) Danish online grocery store. Single-file Python implementation using `requests` for HTTP and `argparse` for CLI parsing.

> Fork of [eisbaw/nemlig_cli](https://github.com/eisbaw/nemlig_cli) — extended with AI meal planning, grocery list management, fridge scanning, a GUI, and plug-and-play multi-provider LLM support.

## Features

### Core shopping
- **Product search** with configurable result limits and retry hints
- **Product details** — full info by product ID
- **Basket management** — view contents, add items with quantity
- **Order history** — list past orders, view order details

### Grocery list management
- **Local grocery list** with persistent storage (`~/.config/nemlig/grocery_list.json`)
- **Add by search term** — `list add "mælk"` searches and adds the top match
- **Budget tracking** — set a budget in kr, see a color-coded progress bar (green/yellow/red)
- **Sync to basket** — push the entire list to your nemlig.com cart in one command

### AI meal planning
- **Guided survey** — interactive questionnaire collects diet, allergies, number of people, meals per day, days, and budget before the AI starts planning
- **Live progress display** — friendly status updates during planning (searching, adding items, budget bar) so you always know what's happening
- **Automatic shopping list** — the AI searches nemlig.com and adds ingredients via function calling / tool use
- **Free-text mode** — skip the survey with `--cli` and describe preferences in your own words instead
- **Recipe import** — pull recipes from a Google Form/Sheet, extract ingredients with AI, and add them to the list
- **Fridge suggestions** — AI analyzes your fridge inventory and suggests what to buy

### Fridge scanner & inventory
- **Real-time camera scanning** with barcode reading (pyzbar) and AI produce detection
- **Barcode lookup** via OpenFoodFacts API — auto-adds recognized products to inventory
- **Color-based produce detection** fallback (banana, apple, orange, tomato, broccoli, etc.)
- **Raspberry Pi AI Camera** support (picamera2 + IMX500 YOLO model)
- **Persistent fridge inventory** (`~/.config/nemlig/inventory.txt`)

### Plug-and-play LLM backends
- **13 providers** out of the box: Azure OpenAI, OpenAI, Anthropic (Claude), Mistral, Groq, Together AI, DeepSeek, xAI, Fireworks, OpenRouter, Ollama, LM Studio, and any custom OpenAI-compatible endpoint
- **Anthropic adapter** — built-in translation layer so Claude works with the same code path (incl. tool calls)
- **Auto-detection** — set one env var and the right provider is picked automatically

### Interactive mode
- **REPL** with tab completion for all commands and subcommands
- Enters automatically when no command is given

## Requirements

- Python >= 3.11
- [uv](https://github.com/astral-sh/uv) package manager
- Credentials for nemlig.com account

Optional (for AI/scanning features):
- `openai` or `anthropic` package — for AI meal planning, recipe import, fridge suggestions
- `opencv-python`, `pyzbar`, `Pillow`, `openfoodfacts` — for barcode/fridge scanning
- `google-api-python-client`, `google-auth-*` — for Google Sheets recipe import
- `picamera2` — for Raspberry Pi AI Camera

```bash
# Set credentials as environment variables
export NEMLIG_USER="your@email.com"
export NEMLIG_PASS="yourpassword"
```

## Usage

All commands are available via the justfile:

```bash
just search "cocio"              # Search products
just details 701025              # Product details
just basket                      # View basket
just add 701025 2                # Add product (quantity optional)
just history                     # Order history
just history 12345678            # Order details
```

Additional commands:

```bash
# Grocery list
just list                        # Show list with budget progress
just list-add "mælk"             # Add by search term
just list-budget 500             # Set budget to 500 kr
just list-sync                   # Push list to nemlig.com basket

# AI meal planning
just plan                        # Guided survey → auto-plan
just plan --cli                  # Free-text chat (skip survey)

# Recipe import (Google Sheets)
just import                      # Import recipes from configured sheet
just import-setup                # Set up Google Sheets OAuth

# Fridge scanner
just scan                        # Start camera scanner
just fridge                      # Show fridge inventory
just fridge-suggest              # AI-powered shopping suggestions
```

Direct execution:

```bash
uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" search "milk"
```

## Architecture

**Single file design**: All logic in `nemlig_cli.py` - a straightforward requests-based client.

**Authentication**: 3-step flow (XSRF token -> Bearer token -> Login). Returns `AuthTokens` dataclass passed to all API functions.

**Dual API endpoints**: Main site API (`nemlig.com/webapi/*`) for auth and basket operations; separate search gateway (`webapi.prod.knl.nemlig.it`) for product search.

See `nemlig_api.md` for complete API documentation including request/response schemas.

## AI Backend Configuration

AI-powered features (meal planning, recipe import, fridge suggestions) are plug-and-play with any supported LLM backend. Set via `AI_PROVIDER` env var or `ai_provider` in `~/.config/nemlig/login.json`. If not set, the provider is auto-detected from available API keys.

| Provider | `AI_PROVIDER` | Required env var | Default model |
|----------|--------------|------------------|---------------|
| Azure OpenAI | `azure` | `AZURE_API_KEY` + `AZURE_ENDPOINT` | `gpt-5.2-2` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o` |
| Anthropic (Claude) | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-5-20250929` |
| Mistral | `mistral` | `MISTRAL_API_KEY` | `mistral-large-latest` |
| Groq | `groq` | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| Together AI | `together` | `TOGETHER_API_KEY` | `Llama-3.3-70B-Instruct-Turbo` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| xAI (Grok) | `xai` | `XAI_API_KEY` | `grok-3` |
| Fireworks AI | `fireworks` | `FIREWORKS_API_KEY` | `llama-v3p3-70b-instruct` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | `openai/gpt-4o` |
| Ollama (local) | `ollama` | *(none — just run Ollama)* | `llama3.2` |
| LM Studio (local) | `lmstudio` | *(none — just run LM Studio)* | `default` |
| Custom endpoint | `custom` | `CUSTOM_BASE_URL` | `default` |

### Quick start (env vars)

```bash
export AI_PROVIDER=openai          # pick your provider
export OPENAI_API_KEY="sk-..."     # set the matching API key
```

Each provider follows the pattern `{PREFIX}_API_KEY` and `{PREFIX}_MODEL` (optional). See `.env.example` for the full list.

### Config file (`~/.config/nemlig/login.json`)

You can also use generic `ai_*` keys that work with any provider:

```json
{
  "username": "your@email.com",
  "password": "yourpassword",
  "ai_provider": "groq",
  "ai_api_key": "gsk_...",
  "ai_model": "llama-3.3-70b-versatile"
}
```

Or provider-specific keys (for backward compatibility):

```json
{
  "ai_provider": "openai",
  "openai_api_key": "sk-...",
  "openai_model": "gpt-4o"
}
```

### Custom / self-hosted endpoints

Any OpenAI-compatible server works with `custom`:

```bash
export AI_PROVIDER=custom
export CUSTOM_BASE_URL="https://your-server.com/v1"
export CUSTOM_API_KEY="your-key"
export CUSTOM_MODEL="your-model"
```

### Dependencies

- Most providers: `uv add openai` (the `openai` package talks to any OpenAI-compatible API)
- Anthropic: `uv add anthropic` (uses the native Anthropic SDK with a built-in adapter)

---

## File Structure

```
nemlig-cli/
├── nemlig_cli.py        # Main CLI — all commands, API client, AI features, fridge scanner
├── server.py            # Local dev / production web server for the meal planner
├── index.html           # Landing / login page
├── meal-planner.html    # Meal planner web UI
├── meal_template.json   # Diet template used by the meal planner
├── justfile             # Command shortcuts
├── pyproject.toml       # Python project config
├── uv.lock              # Locked dependencies
├── .env.example         # Environment variable templates (all providers)
├── deploy-azure.sh      # Azure deploy script for the meal planner
├── CLAUDE.md            # AI assistant instructions
├── README.md            # This file
└── nemlig_api.md        # API documentation
```

## License

MIT
