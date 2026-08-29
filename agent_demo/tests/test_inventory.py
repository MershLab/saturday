"""Unit tests for the inventory library."""
import json

import pytest

from inventory.models import Item
from inventory.service import InventoryService
from inventory import storage


@pytest.fixture()
def svc() -> InventoryService:
    return InventoryService([
        Item("A-1", "Widget", 9.99, 10),
        Item("B-2", "Gadget", 4.50, 3),          # low stock by default (<=5)
        Item("C-3", "Doohickey", 100.0, 0, reorder_level=0),
    ])


# ---------- model validation ----------
def test_item_rejects_negative_price():
    with pytest.raises(ValueError):
        Item("X", "Bad", -1.0, 5)


def test_item_rejects_negative_quantity():
    with pytest.raises(ValueError):
        Item("X", "Bad", 1.0, -5)


def test_item_rejects_blank_sku():
    with pytest.raises(ValueError):
        Item("  ", "Bad", 1.0, 5)


def test_stock_value_rounds_to_cents():
    assert Item("X", "Thing", 3.333, 3).stock_value() == 10.0


# ---------- service behaviour ----------
def test_add_duplicate_sku_raises(svc):
    with pytest.raises(ValueError, match="duplicate"):
        svc.add(Item("A-1", "Copy", 1.0, 1))


def test_get_missing_sku_raises_keyerror(svc):
    with pytest.raises(KeyError):
        svc.get("NOPE")


def test_receive_increases_stock(svc):
    assert svc.receive("A-1", 5).quantity == 15


def test_fulfill_decreases_stock(svc):
    assert svc.fulfill("A-1", 4).quantity == 6


def test_fulfill_refuses_oversell(svc):
    with pytest.raises(ValueError, match="cannot fulfill"):
        svc.fulfill("A-1", 11)


def test_fulfill_and_receive_reject_nonpositive(svc):
    with pytest.raises(ValueError):
        svc.fulfill("A-1", 0)
    with pytest.raises(ValueError):
        svc.receive("A-1", -2)


def test_low_stock_lists_only_reorder_items(svc):
    assert [i.sku for i in svc.low_stock()] == ["B-2"]


def test_total_value(svc):
    # 9.99*10 + 4.50*3 + 100*0 = 99.90 + 13.50 = 113.40
    assert svc.total_value() == 113.40


def test_all_is_sorted_by_sku(svc):
    assert [i.sku for i in svc.all()] == ["A-1", "B-2", "C-3"]


def test_remove_then_get_fails(svc):
    svc.remove("A-1")
    with pytest.raises(KeyError):
        svc.get("A-1")


# ---------- persistence round-trip ----------
def test_save_load_roundtrip(tmp_path, svc):
    path = tmp_path / "inv.json"
    svc.save(str(path))
    loaded = InventoryService.load(str(path))
    assert loaded.all() == svc.all()


def test_saved_file_is_valid_json(tmp_path, svc):
    path = tmp_path / "inv.json"
    svc.save(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 3 and data[0]["sku"] == "A-1"


def test_storage_load_bad_json_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        storage.load(str(bad))
