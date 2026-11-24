#!/usr/bin/env python3
"""Turns a PEM CA bundle into ca_bundle.c (const unsigned char ca_bundle[], ca_bundle_len)."""
import sys

pem = open(sys.argv[1], "rb").read()
if pem.count(b"-----BEGIN CERTIFICATE-----") < 50:
    sys.exit("the CA bundle looks too small")
with open(sys.argv[2], "w") as f:
    f.write("const unsigned char ca_bundle[] = {\n")
    for i in range(0, len(pem), 24):
        f.write("  " + ", ".join(str(b) for b in pem[i:i + 24]) + ",\n")
    f.write("};\nconst unsigned int ca_bundle_len = %d;\n" % len(pem))
