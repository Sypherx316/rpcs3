# What this fork adds to lavavex's PS5 title (run from src/rpcs3 after ps5_upstream_fixups.py):
#  - disc games with PKGDIR/INSDIR/PS3_EXTRA (God of War: Ascension and others) no longer freeze at boot
#  - the self-updater (ps5_updater.cpp), stamped with the RPCS3 version being built
# usage: ps5_extras.py <version, e.g. 0.0.43-20581>
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
version = sys.argv[1]
build = int(version.rsplit("-", 1)[1])


def edit(path, old, new, marker):
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if "\r\n" in text:
        old, new, marker = (s.replace("\n", "\r\n") for s in (old, new, marker))
    if marker in text:
        print(f"{path}: already done")
        return
    assert text.count(old) == 1, f"{path}: expected 1 match for: {old[:70]!r}"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace(old, new))
    print(f"{path}: done")


# Big Picture Mode boots games from the main thread, and the main thread is also the one that serves
# call_from_main_thread. Emulator::Load installs a disc's PKGDIR/INSDIR/PS3_EXTRA packages through
# BlockingCallFromMainThread, which then waited for itself forever. Run such calls in place instead.
edit("rpcs3/Emu/System.cpp",
     "void Emulator::BlockingCallFromMainThread(std::function<void()>&& func, bool track_emu_state, std::source_location src_loc) const\n{\n",
     "void Emulator::BlockingCallFromMainThread(std::function<void()>&& func, bool track_emu_state, std::source_location src_loc) const\n{\n"
     "#ifdef __PROSPERO__\n"
     "\t// PS5 title: the main thread serves the main-thread queue itself, waiting for it here never ends\n"
     "\tif (thread_ctrl::is_main())\n"
     "\t{\n"
     "\t\tfunc();\n"
     "\t\treturn;\n"
     "\t}\n"
     "#endif\n\n",
     "PS5 title: the main thread serves the main-thread queue itself")

# Unmapping a guest shared-memory view (sys_mmapper_unmap_shared_memory) left a hole in the PS5 port,
# while RPCS3 everywhere but Windows keeps the view in place without access ("remnants of the shm until
# overwritten") and only ever unmaps the g_base view, never g_sudo: the two views stopped matching and
# God of War: Ascension read a null pointer right after unmapping its boot buffers. Do what Linux does.
edit("rpcs3/util/vm_native.cpp",
     "#elif defined(__PROSPERO__)\n"
     "\t\t// Give the view back as part of the surrounding reservation\n"
     "\t\tensure(::ps5_shm_unmap(target, m_size, PS5_SHM_KEEP_RESERVED) == 0, \"ps5_shm_unmap failed\");\n",
     "#elif defined(__PROSPERO__)\n"
     "\t\t// As on Linux: keep the view mapped without access, the remnants of the shm stay until overwritten\n"
     "\t\tensure(::sceKernelMprotect(target, m_size, +protection::no) == 0, \"sceKernelMprotect failed\");\n",
     "the remnants of the shm stay until overwritten")

main = "rpcs3/ps5/main_ps5.cpp"

# The PS5 frontend never set on_install_pkgs (what installs a disc's packages): same as RPCS3's own frontend
edit(main,
     "\t\tg_emu_callbacks.on_missing_fw = []() { sys_log.error(\"Missing firmware: put PS3UPDAT.PUP in %s\", data_dir); };\n",
     "\t\tg_emu_callbacks.on_missing_fw = []() { sys_log.error(\"Missing firmware: put PS3UPDAT.PUP in %s\", data_dir); };\n"
     "\t\tg_emu_callbacks.on_install_pkgs = [](const std::vector<std::string>& pkgs, bool from_optical_drive)\n"
     "\t\t{\n"
     "\t\t\tfor (const std::string& pkg : pkgs)\n"
     "\t\t\t{\n"
     "\t\t\t\tsys_log.notice(\"Installing the disc's package %s\", pkg);\n"
     "\t\t\t\tif (!rpcs3::utils::install_pkg(pkg, from_optical_drive))\n"
     "\t\t\t\t{\n"
     "\t\t\t\t\tsys_log.error(\"Failed to install %s\", pkg);\n"
     "\t\t\t\t\treturn false;\n"
     "\t\t\t\t}\n"
     "\t\t\t}\n"
     "\t\t\treturn true;\n"
     "\t\t};\n",
     "g_emu_callbacks.on_install_pkgs")

# The self-updater starts once the title is up
edit(main,
     "#include <condition_variable>\n",
     "#include <condition_variable>\n\nnamespace ps5_updater { void start(); }\n",
     "namespace ps5_updater")
edit(main,
     "\t// The main thread: run what RPCS3 posts until emulation ends\n",
     "\t// Newer builds of this fork install themselves (ps5_updater.cpp)\n"
     "\tps5_updater::start();\n\n"
     "\t// The main thread: run what RPCS3 posts until emulation ends\n",
     "ps5_updater::start();")

edit("rpcs3/ps5/CMakeLists.txt",
     "    ps5_pad_handler.cpp\n",
     "    ps5_pad_handler.cpp\n    ps5_updater.cpp\n",
     "ps5_updater.cpp")
edit("rpcs3/ps5/CMakeLists.txt",
     "3rdparty::stblib 3rdparty::zlib",
     "3rdparty::stblib 3rdparty::libcurl 3rdparty::yaml-cpp 3rdparty::zlib",
     "3rdparty::libcurl 3rdparty::yaml-cpp")

with open(os.path.join(here, "ps5_updater.cpp"), encoding="utf-8") as f:
    source = f.read().replace("@TB2_BUILD@", str(build)).replace("@TB2_VERSION@", version)
with open("rpcs3/ps5/ps5_updater.cpp", "w", encoding="utf-8", newline="\n") as f:
    f.write(source)
print(f"rpcs3/ps5/ps5_updater.cpp: RPCS3 {version}, PS5 build {build}")
