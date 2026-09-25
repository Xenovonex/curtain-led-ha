#!/usr/bin/env python3
"""Decode a "Record BLE log" capture from the bridge.

Usage:  python decode_ble_log.py ~/curtain-ble-logs/ble-YYYYmmdd-HHMMSS.log

Each capture line is:  <epoch> <W|N> <hex>
  W = a frame the bridge wrote to 0xFF01;  N = a notification from 0xFF02.
This strips the 8-byte transport frame and prints the payload + opcode so you
can spot new commands (e.g. the sound/mic mode). See docs/PROTOCOL.md.
"""
import sys, struct
from collections import Counter

KNOWN = {
    "0a71": "power", "0ae00e": "enter-draw", "0ae002": "animation",
    "0ae20b": "solid-color", "0ae206": "draw-bitmap", "0ae205": "hearts",
    "0ae1": "effect-palette", "0aea": "bulk-upload/query", "0a1014": "handshake",
    "0ae231": "status-query",
}

def label(pl):
    for pfx, name in KNOWN.items():
        if pl.startswith(pfx):
            return name
    return "??? UNKNOWN"

def strip_frame(b):
    if len(b) >= 8 and b[0] in (0x01, 0x05) and b[2] == 0x80:
        plen = struct.unpack(">H", b[6:8])[0]
        return b[8:8 + plen]
    return b  # already a payload, or a differently-framed packet

def main(path):
    rows, t0, opcounts = [], None, Counter()
    with open(path, encoding="ascii", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                if line.startswith("#"): print(line)
                continue
            try:
                ts_s, direction, hexv = line.split()
                ts = float(ts_s); raw = bytes.fromhex(hexv)
            except ValueError:
                continue
            if t0 is None: t0 = ts
            pl = strip_frame(raw)
            rows.append((ts - t0, direction, pl.hex()))
    print(f"\n{len(rows)} frames\n{'rel_t':>9}  dir  {'opcode':<16} payload")
    for rel, d, pl in rows:
        lab = label(pl)
        opcounts[lab] += 1
        print(f"{rel:9.3f}  {d:<3}  {lab:<16} {pl}")
    print("\n=== opcode summary (look for ??? UNKNOWN = candidate mic/new command) ===")
    for k, v in opcounts.most_common():
        print(f"  {k:<20} x{v}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    main(sys.argv[1])
