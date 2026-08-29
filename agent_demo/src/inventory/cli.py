"""Command-line interface: `inventory <command> ...`"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import Item
from .service import InventoryService


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inventory", description="Tiny inventory manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="add an item")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("--price", type=float, required=True)
    add.add_argument("--qty", type=int, default=0)
    add.add_argument("--reorder", type=int, default=5)

    recv = sub.add_parser("receive", help="add stock")
    recv.add_argument("sku")
    recv.add_argument("amount", type=int)

    ful = sub.add_parser("fulfill", help="remove stock")
    ful.add_argument("sku")
    ful.add_argument("amount", type=int)

    rm = sub.add_parser("remove", help="delete an item")
    rm.add_argument("sku")

    sub.add_parser("list", help="list all items")
    sub.add_parser("low", help="list items at/below reorder level")

    st = sub.add_parser("stats", help="total stock value + counts")
    p.add_argument("--store", default="inventory.json", help="JSON file backing the inventory")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    svc = InventoryService.load(args.store) if Path(args.store).exists() else InventoryService()

    try:
        if args.cmd == "add":
            svc.add(Item(args.sku, args.name, args.price, args.qty, args.reorder))
            svc.save(args.store)
            print(f"added {args.sku}")
        elif args.cmd == "receive":
            it = svc.receive(args.sku, args.amount)
            svc.save(args.store)
            print(f"{it.sku} now has {it.quantity}")
        elif args.cmd == "fulfill":
            it = svc.fulfill(args.sku, args.amount)
            svc.save(args.store)
            print(f"{it.sku} now has {it.quantity}")
        elif args.cmd == "remove":
            svc.remove(args.sku)
            svc.save(args.store)
            print(f"removed {args.sku}")
        elif args.cmd == "list":
            for it in svc.all():
                flag = "  <-- REORDER" if it.needs_reorder else ""
                print(f"{it.sku:<12} {it.name:<20} ${it.price:>8.2f} x{it.quantity}{flag}")
        elif args.cmd == "low":
            for it in svc.low_stock():
                print(f"{it.sku:<12} {it.name:<20} qty={it.quantity} (reorder <= {it.reorder_level})")
        elif args.cmd == "stats":
            print(json.dumps({
                "items": len(svc.all()),
                "units": sum(i.quantity for i in svc.all()),
                "total_value": svc.total_value(),
            }))
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
