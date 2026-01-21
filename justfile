# Nemlig.com CLI - Grocery Shopping
# Set credentials via environment variables:
#   export NEMLIG_USER="your@email.com"
#   export NEMLIG_PASS="yourpassword"

# Show available commands
default:
    @just --list

# Search for products on nemlig.com
search QUERY:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NEMLIG_USER:-}" ] || [ -z "${NEMLIG_PASS:-}" ]; then
        echo "Error: Set NEMLIG_USER and NEMLIG_PASS environment variables"
        exit 1
    fi
    echo '> uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "***" search "{{QUERY}}"'
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" search "{{QUERY}}"

# Show detailed product information
details PRODUCT_ID:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NEMLIG_USER:-}" ] || [ -z "${NEMLIG_PASS:-}" ]; then
        echo "Error: Set NEMLIG_USER and NEMLIG_PASS environment variables"
        exit 1
    fi
    echo '> uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "***" details "{{PRODUCT_ID}}"'
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" details "{{PRODUCT_ID}}"

# Show current shopping basket
basket:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NEMLIG_USER:-}" ] || [ -z "${NEMLIG_PASS:-}" ]; then
        echo "Error: Set NEMLIG_USER and NEMLIG_PASS environment variables"
        exit 1
    fi
    echo '> uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "***" basket'
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" basket

# Add product to basket (use product ID from search results)
add PRODUCT_ID QUANTITY="1":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NEMLIG_USER:-}" ] || [ -z "${NEMLIG_PASS:-}" ]; then
        echo "Error: Set NEMLIG_USER and NEMLIG_PASS environment variables"
        exit 1
    fi
    echo '> uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "***" add "{{PRODUCT_ID}}" --quantity "{{QUANTITY}}"'
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" add "{{PRODUCT_ID}}" --quantity "{{QUANTITY}}"

# Show order history (optionally with ORDER_ID for details)
history ORDER_ID="":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NEMLIG_USER:-}" ] || [ -z "${NEMLIG_PASS:-}" ]; then
        echo "Error: Set NEMLIG_USER and NEMLIG_PASS environment variables"
        exit 1
    fi
    if [ -z "{{ORDER_ID}}" ]; then
        echo '> uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "***" history'
        uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" history
    else
        echo '> uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "***" history "{{ORDER_ID}}"'
        uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" history "{{ORDER_ID}}"
    fi

# =============================================================================
# GUI Application
# =============================================================================

# Launch GUI application with live camera feed (uses IMX500 NPU)
gui:
    uv run python nemlig_gui.py

# Launch GUI with custom trained model (uses CPU inference - slower but customized)
gui-custom:
    uv run python nemlig_gui.py --custom

# =============================================================================
# Produce Inventory Commands (no login required)
# =============================================================================

# Scan produce with AI camera and update inventory
scan:
    uv run python nemlig_cli.py scan

# Show produce inventory
inventory-show:
    uv run python nemlig_cli.py inventory show

# Add item to inventory manually
inventory-add ITEM QUANTITY="1":
    uv run python nemlig_cli.py inventory add "{{ITEM}}" --quantity "{{QUANTITY}}"

# Remove item from inventory
inventory-remove ITEM QUANTITY="999":
    uv run python nemlig_cli.py inventory remove "{{ITEM}}" --quantity "{{QUANTITY}}"

# Clear all inventory
inventory-clear:
    uv run python nemlig_cli.py inventory clear

# Show shopping list
shopping-list-show:
    uv run python nemlig_cli.py shopping-list show

# Add item to shopping list
shopping-list-add ITEM QUANTITY="1":
    uv run python nemlig_cli.py shopping-list add "{{ITEM}}" --quantity "{{QUANTITY}}"

# Remove item from shopping list
shopping-list-remove ITEM:
    uv run python nemlig_cli.py shopping-list remove "{{ITEM}}"

# Clear shopping list
shopping-list-clear:
    uv run python nemlig_cli.py shopping-list clear

# Add shopping list items to nemlig basket (requires login)
shopping-list-to-basket:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NEMLIG_USER:-}" ] || [ -z "${NEMLIG_PASS:-}" ]; then
        echo "Error: Set NEMLIG_USER and NEMLIG_PASS environment variables"
        exit 1
    fi
    echo '> uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "***" shopping-list to-basket'
    uv run python nemlig_cli.py -u "$NEMLIG_USER" -p "$NEMLIG_PASS" shopping-list to-basket
