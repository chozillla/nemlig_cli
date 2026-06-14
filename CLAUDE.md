# CLAUDE.md

AI assistant guidance for this repository. See README.md for project overview and workflow documentation.

## Quick Commands

```bash
just search "cocio"     # Search products
just details 701025     # Product details
just basket             # View basket
just add 701025 2       # Add product
just history            # Order history
```

Requires `NEMLIG_USER` and `NEMLIG_PASS` environment variables.

## Privacy

Never record actual personal information (real names, addresses, phone numbers, order IDs) when documenting APIs. Replace with realistic placeholder values (e.g., "Anders And", "Vesterbrogade 42", "+4512345678").

## Project Commands

Custom slash commands for this project.

- `/privacy-checker` - Scan files for personal data leaks

## Files

- `nemlig_cli.py` - Single-file Python client
- `nemlig_api.md` - API documentation (source of truth for endpoints)
- `justfile` - Command shortcuts
