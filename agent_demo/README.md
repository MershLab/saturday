# inventory-demo

A tiny inventory management library + CLI, built end-to-end by an autonomous
coding agent as a capability demonstration: scaffold → implement → test →
debug → verify.

## Layout

```
agent_demo/
├── src/inventory/
│   ├── models.py     # frozen dataclass Item with validation + reorder logic
│   ├── service.py    # InventoryService: add/receive/fulfill/remove/stats
│   ├── storage.py    # JSON persistence (auto-creates parent dirs)
│   └── cli.py        # argparse CLI with --store JSON backend
├── tests/            # 17 pytest unit tests
├── smoke_test.py     # 11-check subprocess E2E test of the real CLI
└── pyproject.toml
```

## Usage

```bash
# from agent_demo/, with src on the path (or `pip install -e .`)
python -m inventory.cli add WIDGET-1 "Test Widget" --price 9.99 --qty 10
python -m inventory.cli receive WIDGET-1 5
python -m inventory.cli fulfill WIDGET-1 12
python -m inventory.cli list          # flags items at/below reorder level
python -m inventory.cli low           # only low-stock items
python -m inventory.cli stats         # JSON summary {items, units, total_value}
```

State persists in `inventory.json` (override with `--store path.json`).

## Tests

```bash
python -m pytest -q      # unit tests (17)
python smoke_test.py     # E2E subprocess test (11 checks)
```

## Bugs the test loop caught during development

1. `needs_reorder` flagged qty=0/reorder=0 items; `reorder_level == 0` now
   explicitly disables auto-reordering.
2. `storage.save` crashed when the store's parent directory didn't exist;
   it now creates parent directories.
