#!/usr/bin/env python3
"""Writes a reference_*.json from an untouched PS5SX3 eboot.bin. Two builds are known:
  TB20   (sha256 f207c681...) -> reference_tb20.json  frame pointer kept (TB18.7 .. TB22.1)
  TB24.1 (sha256 830563ba...) -> reference_tb24.json  frame pointer omitted (TB24.1 onwards)
Only needed again if a reference build is ever changed. Usage: make_reference.py <eboot.bin>"""
import hashlib, json, sys
from autopatch import FakeSelf

KNOWN = {
    "f207c6818a93948591ad35d908560d897b8e27a392c0ab07c778186c9333955a": {
        "file": "reference_tb20.json", "build": "PS5SX3 TB20", "frame": None,
        "calc": (0xE62010, 0xE625CA), "raw": (0xE7BC50, 0xE7BD5F),   # the function, then its two lambdas
        "calc_xref": 0xE6235F, "raw_xref": 0xE7BC9A,
        "addresses": {
            "start": 0xE622F0, "join": 0xE623CB, "fail": 0xE623FD, "b_start": 0xE62410, "b_end": 0xE62430,
            "lambda_u32": 0xE62430, "lambda_u16": 0xE62500, "get_address": 0xE61C10,
            "fxo": 0x6E71700, "srcloc": 0x68AF918, "sudo": 0xA14E1A0,
        },
    },
    "830563ba634dff10d4840c5ad73547ad9a3beef46e37c4b31f52b3bb035c79e3": {
        "file": "reference_tb24.json", "build": "PS5SX3 TB24.1", "frame": "rsp",
        "calc": (0xF0A7A0, 0xF0AD3A), "raw": (0xF243E0, 0xF244E9),
        "calc_xref": 0xF0AAC8, "raw_xref": 0xF24425,
        "addresses": {
            "start": 0xF0AA51, "join": 0xF0AB36, "fail": 0xF0AB69, "b_start": 0xF0AB7C, "b_end": 0xF0ABA0,
            "lambda_u32": 0xF0ABA0, "lambda_u16": 0xF0AC70, "get_address": 0xF0A390,
            "fxo": 0x70AAD80, "srcloc": 0x6AA4148, "sudo": 0xA387930,
        },
    },
}

data = open(sys.argv[1], "rb").read()
k = KNOWN.get(hashlib.sha256(data).hexdigest())
assert k, "not one of the known reference builds"
sf = FakeSelf(bytearray(data))
(calc, calc_end), (raw, raw_end) = k["calc"], k["raw"]
ref = {"build": k["build"]}
if k["frame"]:
    ref["frame"] = k["frame"]
ref["calc"] = {
    "va": calc, "code": sf.read(calc, calc_end - calc).hex(), "first_xref": k["calc_xref"],
    "addresses": k["addresses"],
    "fields": {
        "command": 0x14444, "immediate": 0x14448,
        "push_buffer_data": 0x208E30, "push_buffer_size": 0x208E38,
        "index_array_address": 0x181C, "registers": [0x1820, 0x181C, 0x1DAC, 0x1DB0],
    },
}
ref["raw"] = {"va": raw, "code": sf.read(raw, raw_end - raw).hex(), "first_xref": k["raw_xref"]}
json.dump(ref, open(k["file"], "w"), indent=1)
print(k["file"], "written:", len(ref["calc"]["code"]) // 2, "+", len(ref["raw"]["code"]) // 2, "bytes of code")
