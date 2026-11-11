# Resolves the known merge conflicts between lavavex's PS5 branch and newer upstream RPCS3.
# Each entry maps the exact conflict block to its resolution; anything else is left for a human.
import sys

RESOLUTIONS = {
    "3rdparty/CMakeLists.txt": [(
        "<<<<<<< HEAD\n"
        "if(NOT MSVC AND NOT ANDROID AND NOT APPLE AND NOT PS5)\n"
        "=======\n"
        "if(NOT WIN32 AND NOT ANDROID AND NOT APPLE)\n",
        # upstream's WIN32 guard plus the port's PS5 exclusion
        "if(NOT WIN32 AND NOT ANDROID AND NOT APPLE AND NOT PS5)\n",
    )],
}

for path in sys.argv[1:]:
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if "<<<<<<< " not in text:
        print(f"{path}: no conflict")
        continue
    for head, resolved in RESOLUTIONS.get(path, []):
        start = text.find(head)
        if start < 0:
            continue
        end = text.index("\n", text.index(">>>>>>> ", start)) + 1
        text = text[:start] + resolved + text[end:]
    if "<<<<<<< " in text:
        sys.exit(f"{path}: conflict not recognised, resolve it by hand")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(f"{path}: resolved")
