#!/usr/bin/env python3
"""Checks that the built ELF has exactly one blank launch block, as PS5SX3 requires."""
import sys

MARKER, SIZE = b"PS5SX3-INSTALLER-LAUNCH-1", 0x238
d = open(sys.argv[1], "rb").read()
if d[:4] != b"\x7fELF":
    sys.exit("not an ELF file")
blank = [i for i in range(len(d)) if d.startswith(MARKER, i) and not any(d[i + 25:i + SIZE])]
if len(blank) != 1:
    sys.exit(f"{len(blank)} blank launch blocks instead of 1")
print(f"{sys.argv[1]}: {len(d)} bytes, launch block at {blank[0]:#x}")
