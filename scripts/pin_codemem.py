#!/usr/bin/env python3
"""Regenerate the pinned checksums in saturday/codemem.py from upstream.

Run this to bump the vendored codebase-memory-mcp version. It refuses to print
anything unless upstream's checksums.txt matches the digest the GitHub API
reports for it, so a compromised or truncated download cannot become a pin.

    python3 scripts/pin_codemem.py v0.10.8
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.request

WANTED = [
    ("Linux", "x86_64", "codebase-memory-mcp-linux-amd64-portable.tar.gz"),
    ("Linux", "aarch64", "codebase-memory-mcp-linux-arm64-portable.tar.gz"),
    ("Darwin", "x86_64", "codebase-memory-mcp-darwin-amd64.tar.gz"),
    ("Darwin", "arm64", "codebase-memory-mcp-darwin-arm64.tar.gz"),
    ("Windows", "AMD64", "codebase-memory-mcp-windows-amd64.zip"),
]
REPO = "DeusData/codebase-memory-mcp"


def main(tag: str) -> int:
    api = subprocess.run(
        ["gh", "api", f"repos/{REPO}/releases/tags/{tag}"],
        capture_output=True, text=True, check=True).stdout
    assets = {a["name"]: a for a in json.loads(api)["assets"]}
    if "checksums.txt" not in assets:
        print("upstream publishes no checksums.txt; refusing to pin", file=sys.stderr)
        return 1

    url = f"https://github.com/{REPO}/releases/download/{tag}/checksums.txt"
    with urllib.request.urlopen(url, timeout=60) as resp:
        raw = resp.read()
    got = hashlib.sha256(raw).hexdigest()
    want = (assets["checksums.txt"].get("digest") or "").removeprefix("sha256:")
    if want and got != want:
        print(f"checksums.txt digest mismatch: api={want} downloaded={got}", file=sys.stderr)
        return 1

    sums = {}
    for line in raw.decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]

    print(f'VERSION = "{tag}"')
    print("ASSETS = {")
    for system, machine, name in WANTED:
        digest = sums.get(name)
        if not digest:
            print(f"missing checksum for {name}", file=sys.stderr)
            return 1
        print(f'    ("{system}", "{machine}"): (\n        "{name}",\n        "{digest}"),')
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "v0.10.8"))
