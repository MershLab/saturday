"""Typed data model for stock items."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    sku: str          # unique stock keeping unit, e.g. "WIDGET-1"
    name: str
    price: float      # unit price, must be >= 0
    quantity: int     # units on hand, must be >= 0
    reorder_level: int = 5   # at/below this quantity the item needs restocking

    def __post_init__(self) -> None:
        if not self.sku or not self.sku.strip():
            raise ValueError("sku must be a non-empty string")
        if not self.name.strip():
            raise ValueError("name must be a non-empty string")
        if not math.isfinite(self.price) or self.price < 0:
            raise ValueError("price must be a finite number >= 0")
        if self.quantity < 0:
            raise ValueError("quantity must be >= 0")
        if self.reorder_level < 0:
            raise ValueError("reorder_level must be >= 0")

    @property
    def needs_reorder(self) -> bool:
        # reorder_level == 0 disables automatic reordering
        return self.reorder_level > 0 and self.quantity <= self.reorder_level

    def stock_value(self) -> float:
        return round(self.price * self.quantity, 2)

    def with_quantity(self, quantity: int) -> "Item":
        return Item(self.sku, self.name, self.price, quantity, self.reorder_level)
