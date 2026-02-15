# Nemlig.com CLI

Command-line interface for [nemlig.com](https://www.nemlig.com) Danish online grocery store. Single-file Python implementation using `requests` for HTTP and `argparse` for CLI parsing.

## Features

- Product search and details
- Shopping basket management (view, add items)
- Order history viewing

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
```

Direct execution:

```bash
uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" search "milk"
```

## Architecture

**Single file design**: All logic in `nemlig_cli.py` - a straightforward requests-based client.

![API Architecture](arch_api.drawio.svg)

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

## Development Workflow: API Discovery with Chrome DevTools MCP

This project was built by having Claude Code control a real browser to observe and document the nemlig.com API. The technique generalizes to any web application where you need to reverse-engineer an undocumented API.

### Overview

The workflow enables an AI assistant to control a real browser, observe network traffic, and document API behavior - then implement a client based on the documented findings.

![MCP Workflow](mcp-workflow.drawio.svg)

### Self-Contained MCP Setup

The Chrome DevTools MCP integration is fully self-contained:

- `.mcp.json` - MCP server configuration pointing to the wrapper script
- `chrome-devtools-mcp-wrapper.sh` - Nix-shell wrapper ensuring reproducible environment with:
  - Pinned nixpkgs (nixos-25.05) for reproducibility
  - Node.js 22 and Chromium from nix
  - Project-local Chrome profile (`.chrome-profile/`) to avoid tainting global settings
  - Pinned `chrome-devtools-mcp` version (0.10.1)

No global installation required - the wrapper script handles everything.

### API Discovery Process

**Phase 1: Network Traffic Capture**

Human operator directs Claude to:

1. **Open target page** with network recording enabled
2. **Perform the operation** being documented (login, search, add to cart, etc.)
3. **List network requests** to see all HTTP traffic
4. **Get request details** for interesting endpoints (headers, body, response)

Example session:
```
Human: Open nemlig.com and enable network recording. Then login with test credentials and show me the network traffic.

Claude: [Uses Chrome DevTools MCP to navigate, perform login, capture traffic]
        [Lists network requests, identifies auth flow]
        [Documents the 3-step auth: AntiForgery -> Token -> login]
```

**Phase 2: Documentation**

Claude analyzes captured traffic and documents:
- Request URLs, methods, headers
- Request/response body structure
- Authentication requirements
- Parameter meanings

This builds up `nemlig_api.md` incrementally.

**Phase 3: Implementation**

Based on the documented API:
1. Claude implements Python functions matching documented endpoints
2. Human tests implementation against real site
3. Debug issues using Chrome DevTools MCP as grounding (compare browser vs client behavior)

### Context Management

**Important**: MCP tool calls return large responses (>25KB for page snapshots/network dumps). To manage context window size:

- Run all MCP interactions from a **sub-agent** (Task tool with explore or general-purpose agent)
- Sub-agent summarizes findings and returns only relevant info
- Main conversation stays focused on implementation

Example pattern:
```
Human: Document the basket API

Claude: [Spawns sub-agent to handle MCP interactions]

Sub-agent: [Opens page, enables recording, adds item to basket]
           [Captures AddToBasket request/response]
           [Returns summary: endpoint, headers, body format, response structure]

Claude: [Updates nemlig_api.md with documented endpoint]
```

### Debugging with Browser Grounding

When the Python client behaves differently than expected:

1. Perform same operation in browser via MCP
2. Compare exact request headers/body
3. Identify missing headers, wrong parameter format, etc.
4. Fix client implementation

This provides a reliable reference for expected API behavior.

### File Structure

```
nemlig-cli/
├── .mcp.json                       # MCP server configuration
├── chrome-devtools-mcp-wrapper.sh  # Nix-shell wrapper for MCP server
├── .chrome-profile/                # Local browser profile (gitignored)
├── arch_api.drawio.svg             # API architecture diagram
├── mcp-workflow.drawio.svg         # MCP workflow diagram
├── nemlig_api.md                   # API documentation (built via workflow)
├── nemlig_cli.py                   # Python client implementation
├── justfile                        # Command shortcuts
├── pyproject.toml                  # Python project config
└── CLAUDE.md                       # AI assistant instructions
```

## License

MIT
