#!/usr/bin/env python3
"""Compares two PS5SX3 packages (.tb2) entry by entry, to check a rebuild against what is published.

    PS5_KEY=... python compare.py published.tb2 rebuilt.tb2

Both are decrypted in memory (nothing is written in the clear). Exit code 0 when the eboot.bin and every
other entry match; the console-side fetcher may differ (it is rebuilt with the runner's current CA
certificates), so a difference there is reported but not counted.
"""
import hashlib
import io
import os
import sys
import zipfile

from build_update import EBOOT, INSTALLER, decrypt


def entries(path, key):
    plain = decrypt(open(path, "rb").read(), key)
    with zipfile.ZipFile(io.BytesIO(plain)) as z:
        listed = [(i.filename, i.CRC, i.file_size) for i in z.infolist()]
        eboot = hashlib.sha256(z.read(EBOOT)).hexdigest()
    return hashlib.sha256(plain).hexdigest(), listed, eboot


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    key = os.environ["PS5_KEY"]
    a_sha, a, a_eboot = entries(sys.argv[1], key)
    b_sha, b, b_eboot = entries(sys.argv[2], key)
    print(f"zip sha256: published {a_sha}")
    print(f"            rebuilt   {b_sha}")
    print(f"eboot.bin:  published {a_eboot}")
    print(f"            rebuilt   {b_eboot}")
    if [x[0] for x in a] != [x[0] for x in b]:
        print("DIFFERENT: the two zips list different files")
        return 1
    bad = 0
    for (name, crc_a, size_a), (_, crc_b, size_b) in zip(a, b):
        if (crc_a, size_a) != (crc_b, size_b):
            print(f"{'differs (fetcher rebuilt)' if name == INSTALLER else 'DIFFERENT'}: {name}")
            bad += name != INSTALLER
    print(f"{len(a)} entries, {'all the same' if not bad else f'{bad} different'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
