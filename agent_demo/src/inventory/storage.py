"""JSON-file backed persistence."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Item


def save(items: list[Item], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {"sku": i.sku, "name": i.name, "price": i.price,
         "quantity": i.quantity, "reorder_level": i.reorder_level}
        for i in items
    ]
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load(path: str | Path) -> list[Item]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Item(**record) for record in raw]
