# Nemlig.com CLI
# Credentials are loaded from .env (cp .env.example .env)
#
# Typical workflow:
#   1. just plan              → AI builds a weekly shopping list from your diet template
#   2. just list              → review what it picked
#   3. just sync              → push the list to your nemlig.com basket
#
# Run `just` (no args) for the grouped command list.

set dotenv-load

# Show available commands grouped by purpose
default:
    @just --list --unsorted

# ─────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────

# Verify nemlig.com credentials are loaded from .env (used as a dependency)
[private]
_auth:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NEMLIG_USER:-}" ] || [ -z "${NEMLIG_PASS:-}" ]; then
        echo "Error: NEMLIG_USER and NEMLIG_PASS must be set in .env" >&2
        exit 1
    fi

# ─────────────────────────────────────────────────────────────
#  1. PLAN — AI builds your shopping list
# ─────────────────────────────────────────────────────────────

# 🤖 AI meal planner — guided survey + diet template (--cli for free chat, --no-template to skip)
[group('1. plan')]
plan *FLAGS:
    uv run python nemlig_cli.py plan {{FLAGS}}

# 📐 Show your active diet template (meal_template.json)
[group('1. plan')]
template:
    uv run python nemlig_cli.py template

# ─────────────────────────────────────────────────────────────
#  2. LIST — review and edit your local shopping list
# ─────────────────────────────────────────────────────────────

# 📋 Show shopping list with budget status
[group('2. list')]
list:
    uv run python nemlig_cli.py list

# Add item to shopping list (by name or product ID)
[group('2. list')]
list-add QUERY QUANTITY="1": _auth
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" list add "{{QUERY}}" --quantity "{{QUANTITY}}"

# Remove item from shopping list
[group('2. list')]
list-remove PRODUCT_ID:
    uv run python nemlig_cli.py list remove "{{PRODUCT_ID}}"

# Clear all items from shopping list
[group('2. list')]
list-clear:
    uv run python nemlig_cli.py list clear

# 💰 Show or set weekly budget (in kr)
[group('2. list')]
budget AMOUNT="":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{AMOUNT}}" ]; then
        uv run python nemlig_cli.py list budget
    else
        uv run python nemlig_cli.py list budget "{{AMOUNT}}"
    fi

# 🔄 Sync shopping list → nemlig.com basket
[group('2. list')]
sync: _auth
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" list sync

# ─────────────────────────────────────────────────────────────
#  3. BROWSE — search + inspect products on nemlig.com
# ─────────────────────────────────────────────────────────────

# 🔍 Search nemlig.com products
[group('3. browse')]
search QUERY: _auth
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" search "{{QUERY}}"

# 📦 Show product details by ID
[group('3. browse')]
product PRODUCT_ID: _auth
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" details "{{PRODUCT_ID}}"

# ─────────────────────────────────────────────────────────────
#  4. BASKET — your live cart + order history on nemlig.com
# ─────────────────────────────────────────────────────────────

# 🛒 Show your live basket on nemlig.com
[group('4. basket')]
basket: _auth
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" basket

# Add product directly to nemlig.com basket (skips the local list)
[group('4. basket')]
basket-add PRODUCT_ID QUANTITY="1": _auth
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" add "{{PRODUCT_ID}}" --quantity "{{QUANTITY}}"

# 📜 Show order history (pass an ID for full order details)
[group('4. basket')]
orders ORDER_ID="": _auth
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{ORDER_ID}}" ]; then
        uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" history
    else
        uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" history "{{ORDER_ID}}"
    fi

# ─────────────────────────────────────────────────────────────
#  5. FRIDGE — track what you already have at home
# ─────────────────────────────────────────────────────────────

# 🧊 Show fridge inventory
[group('5. fridge')]
fridge:
    uv run python nemlig_cli.py fridge

# 📷 Scan fridge with camera (barcodes + AI detection)
[group('5. fridge')]
fridge-scan:
    uv run python nemlig_cli.py scan

# 🤖 AI meal suggestions based on fridge contents
[group('5. fridge')]
fridge-suggest:
    uv run python nemlig_cli.py fridge suggest

# Clear fridge inventory
[group('5. fridge')]
fridge-clear:
    uv run python nemlig_cli.py fridge clear

# ─────────────────────────────────────────────────────────────
#  6. IMPORT — pull recipes from a Google Form/Sheet
# ─────────────────────────────────────────────────────────────

# 📥 Import recipes from Google Form/Sheet (pass spreadsheet ID, or run with no args after init)
[group('6. import')]
import SPREADSHEET_ID="":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{SPREADSHEET_ID}}" ]; then
        uv run python nemlig_cli.py import
    else
        uv run python nemlig_cli.py import "{{SPREADSHEET_ID}}"
    fi

# First-time Google Sheets setup
[group('6. import')]
import-init:
    uv run python nemlig_cli.py import --setup

# ─────────────────────────────────────────────────────────────
#  7. WEB — the meal planner web app (server.py)
# ─────────────────────────────────────────────────────────────

# 🌐 Start local web app at http://localhost:8000/meal-planner
[group('7. web')]
web:
    uv run python server.py

# Deploy web app to production (ugemad.dk)
[group('7. web')]
web-deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git status --porcelain server.py index.html meal-planner.html)" ]; then
        echo "⚠ Uncommitted changes in server files. Commit first or they won't be deployed."
        echo "  Changed: $(git status --porcelain server.py index.html meal-planner.html | awk '{print $2}' | tr '\n' ' ')"
        read -p "Deploy anyway from working directory? [y/N] " yn
        case "$yn" in [Yy]*) ;; *) echo "Aborted."; exit 1;; esac
    fi
    bash deploy-azure.sh

# Show production web app logs
[group('7. web')]
web-logs CONTAINER="server":
    az container logs -g rg-n8n -n mealplanner --container-name {{CONTAINER}}

# Restart production web app
[group('7. web')]
web-restart:
    az container restart -g rg-n8n -n mealplanner

# ─────────────────────────────────────────────────────────────
#  8. MISC
# ─────────────────────────────────────────────────────────────

# 💬 Interactive menu (browse + basket + history in one TUI)
[group('8. misc')]
menu:
    uv run python nemlig_cli.py

# ─────────────────────────────────────────────────────────────
#  Aliases — keep old command names working
# ─────────────────────────────────────────────────────────────

alias dev := web
alias deploy := web-deploy
alias logs := web-logs
alias restart := web-restart
alias details := product
alias history := orders
alias scan := fridge-scan
alias nemlig := menu
alias add := basket-add
alias list-sync := sync
alias import-setup := import-init
