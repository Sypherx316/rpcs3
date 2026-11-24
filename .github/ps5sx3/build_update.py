#!/usr/bin/env python3
"""Builds the update this repository publishes for PS5SX3.

1. Reads the developer's latest.json (the same file PS5SX3 itself asks for).
2. Downloads that build's zip and checks its size and SHA-256.
3. Applies the The Bigs 2 fix to PPSA99303/eboot.bin (autopatch.py) and rebuilds the zip with every
   other entry untouched.
4. Writes, into --out:
     latest.json             what PS5SX3 and its installer read: the size and SHA-256 are those of the
                             fixed zip, so the installer accepts it once it is on the console
     PS5SX3-<build>.zip.tb2  the fixed zip, encrypted and signed with PS5_KEY
     build-info.json         what was done, for the release notes

The zip is never published in the clear. File format of .tb2, the same as the RPCS3 PS5 package:
"TB2PS5" 01 00 | 16-byte counter | AES-256-CTR(zip) | HMAC-SHA256 of everything before it.

Exit codes: 0 built, 10 nothing new (--have is the developer's current build), 2 refused / failed.
"""
import argparse
import hashlib
import hmac
import io
import json
import os
import re
import sys
import urllib.request
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from autopatch import AlreadyFixed, Refused, autopatch

UPSTREAM = "https://ps5sx3-updates.ps5sx2.workers.dev/latest.json"
EBOOT = "PPSA99303/eboot.bin"
INSTALLER = "PPSA99303/PS5SX3-Installer.elf"
STOCK = "PPSA99303/PS5SX3-Installer-stock.elf"
AGENT = {"User-Agent": "ps5sx3-thebigs2-autofix"}


def fetch(url, limit):
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=120) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SystemExit(f"{url} is larger than expected")
    return data


def read_upstream(url):
    info = json.loads(fetch(url, 65536))
    if not isinstance(info, dict):
        raise SystemExit("the developer's latest.json is not an object")
    build, number, hotfix = info.get("build"), info.get("number"), info.get("hotfix", 0)
    if not (isinstance(build, str) and re.fullmatch(r"TB\d+(\.\d+)?", build) and isinstance(number, int)
            and isinstance(hotfix, int)):
        raise SystemExit(f"unexpected build name in the developer's latest.json: {build!r}")
    if not (isinstance(info.get("zip"), str) and info["zip"].startswith("https://")):
        raise SystemExit("the developer's latest.json has no https:// zip address")
    if not (isinstance(info.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", info["sha256"])):
        raise SystemExit("the developer's latest.json has no SHA-256")
    if not (isinstance(info.get("size"), int) and 1_000_000 < info["size"] < 400_000_000):
        raise SystemExit("the developer's latest.json has an implausible size")
    return info


def rebuild_zip(original, replacements, additions):
    """A copy of the zip with some entries' contents replaced; names, order, dates and modes are kept.
    additions: {new name: (data, existing entry whose date and mode it takes)}, placed after that entry."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, zipfile.ZipFile(out, "w") as dst:
        def put(name, data, like):
            info = zipfile.ZipInfo(name, like.date_time)
            info.compress_type = like.compress_type
            info.external_attr = like.external_attr
            info.create_system = like.create_system
            dst.writestr(info, data, compresslevel=9 if like.compress_type == zipfile.ZIP_DEFLATED else None)
        for item in src.infolist():
            data = replacements.get(item.filename)
            put(item.filename, src.read(item) if data is None else data, item)
            for name, (extra, after) in additions.items():
                if after == item.filename:
                    put(name, extra, item)
    return out.getvalue()


def encrypt(plain, key_hex):
    if not re.fullmatch(r"[0-9a-fA-F]{128}", key_hex):
        raise SystemExit("PS5_KEY must be 128 hex characters (AES-256 key, then HMAC key)")
    aes_key, mac_key = bytes.fromhex(key_hex[:64]), bytes.fromhex(key_hex[64:])
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
    body = b"TB2PS5\x01\x00" + iv + enc.update(plain) + enc.finalize()
    return body + hmac.new(mac_key, body, hashlib.sha256).digest()


def decrypt(package, key_hex):
    """The console's side, to check each package before it is published."""
    body, tag = package[:-32], package[-32:]
    if not hmac.compare_digest(hmac.new(bytes.fromhex(key_hex[64:]), body, hashlib.sha256).digest(), tag):
        raise SystemExit("the package's HMAC does not verify")
    dec = Cipher(algorithms.AES(bytes.fromhex(key_hex[:64])), modes.CTR(body[8:24])).decryptor()
    return dec.update(body[24:]) + dec.finalize()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--zip-base", required=True,
                    help="https:// folder the release files are served from; {build} is replaced by the build name")
    ap.add_argument("--have", default="", help="build already published here; nothing is done if it is still current")
    ap.add_argument("--upstream", default=UPSTREAM)
    ap.add_argument("--zip-file", help="use this local copy of the developer's zip instead of downloading it")
    ap.add_argument("--installer", required=True,
                    help="the console-side fetcher (installer/PS5SX3-Installer.elf); it takes the installer's "
                         "name and the developer's installer is kept beside it as PS5SX3-Installer-stock.elf")
    args = ap.parse_args()

    up = read_upstream(args.upstream)
    build = up["build"]
    print(f"developer's build: {build} ({up['size']} bytes, sha256 {up['sha256']})")
    if args.have == build:
        print("already published here: nothing to do")
        return 10

    original = open(args.zip_file, "rb").read() if args.zip_file else fetch(up["zip"], up["size"])
    if len(original) != up["size"] or hashlib.sha256(original).hexdigest() != up["sha256"]:
        raise SystemExit("the downloaded zip does not match the developer's size / SHA-256")
    with zipfile.ZipFile(io.BytesIO(original)) as z:
        if EBOOT not in z.namelist():
            raise SystemExit(f"the zip has no {EBOOT}")
        eboot = z.read(EBOOT)
        if INSTALLER not in z.namelist() or STOCK in z.namelist():
            raise SystemExit(f"the zip's installer is not where expected ({INSTALLER})")
        stock = z.read(INSTALLER)
    fetcher = open(args.installer, "rb").read()
    if fetcher[:4] != b"ELF" or fetcher == stock:
        raise SystemExit("--installer is not the fetcher ELF")

    swap = {INSTALLER: fetcher}
    extra = {STOCK: (stock, INSTALLER)}
    try:
        fixed, report = autopatch(eboot)
        final = rebuild_zip(original, {EBOOT: fixed, **swap}, extra)
        note = "The Bigs 2 crowd fix applied"
    except AlreadyFixed as e:
        # The developer shipped the fix: the eboot is passed through, the fetcher still goes in so the
        # console keeps following this feed.
        report = {"status": "already_fixed", "reason": str(e)}
        final, note = rebuild_zip(original, swap, extra), "fix already in this build"
    except Refused as e:
        print(f"REFUSED: this build is not recognised, nothing is published.\n  {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1))

    # Everything except the eboot and the installer must come out of the new zip byte for byte
    with zipfile.ZipFile(io.BytesIO(original)) as a, zipfile.ZipFile(io.BytesIO(final)) as b:
        expected = a.namelist()
        expected.insert(expected.index(INSTALLER) + 1, STOCK)
        if b.namelist() != expected:
            raise SystemExit("the rebuilt zip lists different files")
        for name in a.namelist():
            if name not in (EBOOT, INSTALLER) and a.getinfo(name).CRC != b.getinfo(name).CRC:
                raise SystemExit(f"the rebuilt zip changed {name}")
        if b.read(INSTALLER) != fetcher or b.read(STOCK) != stock:
            raise SystemExit("the installers did not go in as intended")
        if b.testzip() is not None:
            raise SystemExit("the rebuilt zip is damaged")

    os.makedirs(args.out, exist_ok=True)
    zip_name = f"PS5SX3-{build}.zip"
    package = encrypt(final, os.environ.get("PS5_KEY", ""))
    if decrypt(package, os.environ["PS5_KEY"]) != final:
        raise SystemExit("the package does not decrypt back to the zip")
    with open(os.path.join(args.out, zip_name + ".tb2"), "wb") as f:
        f.write(package)
    notes = (up.get("notes") or "").strip()
    latest = {
        "build": build, "number": up["number"], "hotfix": up.get("hotfix", 0),
        "zip": f"{args.zip_base.format(build=build).rstrip('/')}/{zip_name}",
        "size": len(final), "sha256": hashlib.sha256(final).hexdigest(),
        "notes": (notes + " | " if notes else "") + note,
    }
    with open(os.path.join(args.out, "latest.json"), "w", newline="\n") as f:
        json.dump(latest, f, indent=1)
        f.write("\n")
    info = {"build": build, "developer_zip_sha256": up["sha256"], "published_zip_sha256": latest["sha256"],
            "package": zip_name + ".tb2", "patch": report}
    with open(os.path.join(args.out, "build-info.json"), "w", newline="\n") as f:
        json.dump(info, f, indent=1)
    print(f"built {zip_name}.tb2 ({note}); zip sha256 {latest['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
