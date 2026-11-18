# Resolves the known conflicts left by applying lavavex's PS5 changes (git apply --3way) onto newer
# upstream RPCS3. "ours" is upstream, "theirs" is the PS5 port. Each rule recognises one conflict block
# and gives its resolution; anything else is left for a human.
import sys


def cmake_glew_guard(ours, theirs):
    # Upstream changed the GLEW guard from MSVC to WIN32, the port added PS5: both
    if ours.strip() == "if(NOT WIN32 AND NOT ANDROID AND NOT APPLE)" and "AND NOT PS5" in theirs:
        return "if(NOT WIN32 AND NOT ANDROID AND NOT APPLE AND NOT PS5)\n"
    return None


def vk_preload_shaders(ours, theirs):
    # Upstream moved shader preloading into preload_shaders() and only calls it when a game boots, which
    # is what the port's "no interpreter precompile for the menu" did: upstream's version as it is
    if ours.startswith("void VKGSRender::preload_shaders()") and "hosting_game" in theirs:
        return ours
    return None


def jit_advancing_reserve(ours, theirs):
    # Upstream now reserves each JIT memory manager at an advancing fixed address and retries until one
    # is free. On the PS5 a refused fixed reservation would make that loop spin forever, and the port's
    # memory_reserve already places JIT memory in its own advancing range: keep the port's call there
    if "s_min_memory_reserve_addr" in ours and "JIT memory manager: out of address space" in theirs:
        return "#ifdef __PROSPERO__\n" + theirs + "#else\n" + ours.rstrip("\n") + "\n#endif\n\n"
    return None


def vm_memory_reset_removed(ours, theirs):
    # Upstream removed utils::memory_reset() (nothing calls it any more), the port had added a PS5 branch
    # to it: upstream's version as it is. The port's PS5 branch of memory_decommit merges on its own
    if not ours.strip() and "void memory_reset(" in theirs:
        return ours
    return None


RULES = {
    "3rdparty/CMakeLists.txt": [cmake_glew_guard],
    "rpcs3/Emu/RSX/VK/VKGSRender.cpp": [vk_preload_shaders],
    "Utilities/JITLLVM.cpp": [jit_advancing_reserve],
    "rpcs3/util/vm_native.cpp": [vm_memory_reset_removed],
}


def resolve(path, text):
    out, pos = [], 0
    while True:
        start = text.find("<<<<<<< ", pos)
        if start < 0:
            out.append(text[pos:])
            return "".join(out)
        mid = text.index("\n=======\n", start)
        end = text.index("\n>>>>>>> ", mid)
        end_line = text.index("\n", end + 1) + 1
        ours = text[text.index("\n", start) + 1:mid + 1]
        theirs = text[mid + len("\n=======\n"):end + 1]
        for rule in RULES.get(path, []):
            resolved = rule(ours, theirs)
            if resolved is not None:
                out.append(text[pos:start] + resolved)
                break
        else:
            out.append(text[pos:end_line])  # unknown: keep the markers
        pos = end_line


failed = False
for path in sys.argv[1:]:
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if "<<<<<<< " not in text:
        print(f"{path}: no conflict")
        continue
    crlf = "\r\n" in text
    text = resolve(path, text.replace("\r\n", "\n"))
    if crlf:
        text = text.replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if "<<<<<<< " in text:
        print(f"{path}: conflict not recognised, resolve it by hand")
        failed = True
    else:
        print(f"{path}: resolved")
sys.exit(1 if failed else 0)
