"""Business logic: add/remove/adjust stock with validation."""
from __future__ import annotations

from .models import Item
from . import storage


class InventoryService:
    def __init__(self, items: list[Item] | None = None) -> None:
        self._items: dict[str, Item] = {}
        for item in items or []:
            self.add(item)

    # -- queries -------------------------------------------------------
    def get(self, sku: str) -> Item:
        try:
            return self._items[sku]
        except KeyError:
            raise KeyError(f"no item with sku {sku!r}") from None

    def all(self) -> list[Item]:
        return sorted(self._items.values(), key=lambda i: i.sku)

    def low_stock(self) -> list[Item]:
        return [i for i in self.all() if i.needs_reorder]

    def total_value(self) -> float:
        return round(sum(i.stock_value() for i in self._items.values()), 2)

    # -- mutations -----------------------------------------------------
    def add(self, item: Item) -> None:
        if item.sku in self._items:
            raise ValueError(f"duplicate sku {item.sku!r}")
        self._items[item.sku] = item

    def remove(self, sku: str) -> None:
        self.get(sku)  # raises if missing
        del self._items[sku]

    def receive(self, sku: str, amount: int) -> Item:
        """Add stock (amount > 0)."""
        if amount <= 0:
            raise ValueError("receive amount must be > 0")
        item = self.get(sku)
        new_item = item.with_quantity(item.quantity + amount)
        self._items[sku] = new_item
        return new_item

    def fulfill(self, sku: str, amount: int) -> Item:
        """Remove stock; refuses to oversell."""
        if amount <= 0:
            raise ValueError("fulfill amount must be > 0")
        item = self.get(sku)
        if amount > item.quantity:
            raise ValueError(
                f"cannot fulfill {amount} of {sku}: only {item.quantity} on hand"
            )
        new_item = item.with_quantity(item.quantity - amount)
        self._items[sku] = new_item
        return new_item

    # -- persistence ---------------------------------------------------
    def save(self, path: str) -> None:
        storage.save(self.all(), path)

    @classmethod
    def load(cls, path: str) -> "InventoryService":
        return cls(storage.load(path))
