# Nemlig.com CLI

Command-line interface for [nemlig.com](https://www.nemlig.com) Danish online grocery store. Single-file Python implementation using `requests` for HTTP and `argparse` for CLI parsing.

> Fork of [eisbaw/nemlig_cli](https://github.com/eisbaw/nemlig_cli) — extended with macro-aware search, grocery list management, and recipe browsing.

## Features

### Core shopping
- **Product search** with configurable result limits
- **Product details** — full info by product ID
- **Basket management** — view contents, add items with quantity
- **Order history** — list past orders, view order details

### Nutrition & recipes
- **Per-100g macros** for top search results (`nemlig macros "kyllingebryst"`)
- **Recipe search** on nemlig.com (`nemlig recipes "chili"`)
- **List-driven recipes** — suggests nemlig recipes that use items already on your list (`nemlig list-recipes`)

### Grocery list management
- **Local grocery list** with persistent storage (`~/.config/nemlig/grocery_list.json`)
- **Add by search term** — `list add "mælk"` searches and adds the top match
- **Budget tracking** — set a budget in kr, see a color-coded progress bar (green/yellow/red)
- **Sync to basket** — push the entire list to your nemlig.com cart in one command

### Interactive mode
- **REPL** with tab completion for all commands and subcommands
- Enters automatically when no command is given

## AI features → Claude Code

This CLI has no LLM inside it. Agentic flows live in **Claude Code** in two complementary ways:

**1. MCP server (`nemlig_mcp.py`)** — exposes every operation as a first-class MCP tool. Once registered, Claude Code can drive nemlig.com from any session without typing a skill prefix. Register once at user scope:

```bash
claude mcp add --scope user nemlig -- uv --directory $(pwd) run python nemlig_mcp.py
```

13 tools available: `search`, `details`, `macros`, `recipes`, `basket`, `basket_add`, `history`, `list_show`, `list_add`, `list_remove`, `list_clear`, `list_budget`, `list_sync`.

**2. Skills (`/nemlig`, `/nemlig-plan`)** — orchestrated multi-step flows when you want the workflow encoded:

- `/nemlig-plan` — weekly meal plan against your diet template (delegates to Sonnet 4.6)
- `/nemlig` — natural-language dispatcher with smart picks (delegates to Haiku 4.5)

Both skills can call MCP tools directly when the server is registered. No API keys required in this repo — Claude reasoning happens inside Claude Code, not inside the CLI process.

## Requirements

- Python >= 3.11
- [uv](https://github.com/astral-sh/uv) package manager
- Credentials for nemlig.com account

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
just macros "kyllingebryst"      # Per-100g macros for top results
just recipes "chili"             # nemlig.com recipes
just list-recipes                # Recipes that use items on your list
```

Grocery list:

```bash
just list                        # Show list with budget progress
just list-add "mælk"             # Add by search term
just list-budget 500             # Set budget to 500 kr
just list-sync                   # Push list to nemlig.com basket
```

Direct execution:

```bash
uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" search "milk"
```

## Architecture

**Single file design**: All logic in `nemlig_cli.py` — a straightforward requests-based client.

**Authentication**: 3-step flow (XSRF token → Bearer token → Login). Returns `AuthTokens` dataclass passed to all API functions.

**Dual API endpoints**: Main site API (`nemlig.com/webapi/*`) for auth and basket operations; separate search gateway (`webapi.prod.knl.nemlig.it`) for product search.

See `nemlig_api.md` for complete API documentation including request/response schemas.

## File Structure

```
nemlig-cli/
├── nemlig_cli.py        # Python client + interactive CLI
├── nemlig_mcp.py        # MCP server exposing operations as tools for Claude Code
├── meal_template.json   # Diet/macro template
├── justfile             # Command shortcuts
├── pyproject.toml       # Python project config
├── uv.lock              # Locked dependencies
├── .env.example         # Environment variable template
├── CLAUDE.md            # AI assistant instructions
├── README.md            # This file
└── nemlig_api.md        # API documentation
```

## License

MIT
