# Build fixes the PS5 port needs on newer upstream RPCS3 (run from src/rpcs3 after the merge).
# Upstream reworked RSX in October 2026 (RSXRendererFactory, display_output); lavavex's frontend
# predates that, so it is adapted here.


def edit(path, old, new, marker, count=1):
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if "\r\n" in text:  # some upstream files are stored with CRLF
        old, new, marker = (s.replace("\n", "\r\n") for s in (old, new, marker))
    if marker in text:
        print(f"{path}: already fixed")
        return
    assert text.count(old) == count, f"{path}: expected {count} match(es) for: {old[:70]!r}"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace(old, new))
    print(f"{path}: fixed")


# Upstream links 3rdparty::hidapi into rpcs3_emu, so hidapi's libusb backend is now built for the PS5.
# The PS5 compiler reports an old __FreeBSD__, and hid.c then redefines libusb_get_string_descriptor,
# a fallback meant only for FreeBSD < 10 (libusb.h already has it inline).
edit("3rdparty/hidapi/CMakeLists.txt",
     "\tadd_subdirectory(hidapi EXCLUDE_FROM_ALL)\n",
     "\tadd_subdirectory(hidapi EXCLUDE_FROM_ALL)\n"
     "\n"
     "\tif(PS5)\n"
     "\t\t# The PS5 compiler reports an old __FreeBSD__; hid.c would redefine libusb_get_string_descriptor (FreeBSD < 10 only)\n"
     "\t\ttarget_compile_options(hidapi_libusb PRIVATE -U__FreeBSD__ -D__FreeBSD__=11)\n"
     "\tendif()\n",
     "hidapi_libusb PRIVATE -U__FreeBSD__")

# Two libc functions the PS5 title cannot import: the SDK's libc has no wcsdup (hid.c, hidapi's
# libusb backend, calls it), and its fnmatch (called by another 3rd-party library; curl's configure
# finds it) is only a stub no module a title loads exports, which link-title.sh refuses.
# hidapi_libusb, linked as an archive ahead of the SDK stubs, carries both.
with open("3rdparty/hidapi/ps5_libc_gaps.c", "w", encoding="utf-8", newline="\n") as f:
    f.write(r"""// libc functions the PS5 title cannot import: the SDK's libc has no wcsdup, and its fnmatch is
// only a stub that no module a title loads exports
#include <fnmatch.h>
#include <stdlib.h>

wchar_t* wcsdup(const wchar_t* s)
{
	size_t len = 0;
	while (s[len])
		len++;
	wchar_t* copy = malloc((len + 1) * sizeof(wchar_t));
	if (copy)
		for (size_t i = 0; i <= len; i++)
			copy[i] = s[i];
	return copy;
}

static int gap_fold(int c, int flags)
{
#ifdef FNM_CASEFOLD
	if ((flags & FNM_CASEFOLD) && c >= 'A' && c <= 'Z')
		return c - 'A' + 'a';
#endif
	(void)flags;
	return c;
}

// 1 or 0 for the bracket expression at *pp, which is then skipped; -1 if there is none ('[' is literal)
static int gap_bracket(const char** pp, int c, int flags)
{
	const char* p = *pp + 1;
	const int negate = *p == '!' || *p == '^';
	int matched = 0;
	p += negate;
	for (int first = 1; *p && (first || *p != ']'); first = 0)
	{
		int lo = (unsigned char)*p++;
		if (lo == '\\' && !(flags & FNM_NOESCAPE) && *p)
			lo = (unsigned char)*p++;
		int hi = lo;
		if (*p == '-' && p[1] && p[1] != ']')
		{
			hi = (unsigned char)p[1];
			p += 2;
			if (hi == '\\' && !(flags & FNM_NOESCAPE) && *p)
				hi = (unsigned char)*p++;
		}
		if (gap_fold(lo, flags) <= gap_fold(c, flags) && gap_fold(c, flags) <= gap_fold(hi, flags))
			matched = 1;
	}
	if (*p != ']')
		return -1;
	*pp = p + 1;
	return matched != negate;
}

static int gap_match(const char* p, const char* s, const char* start, int flags)
{
	for (;;)
	{
		const int c = (unsigned char)*s;
		const int slash = c == '/' && (flags & FNM_PATHNAME);
		const int period = c == '.' && (flags & FNM_PERIOD) && (s == start || ((flags & FNM_PATHNAME) && s[-1] == '/'));
		switch (*p)
		{
		case '\0':
#ifdef FNM_LEADING_DIR
			if ((flags & FNM_LEADING_DIR) && c == '/')
				return 0;
#endif
			return c ? FNM_NOMATCH : 0;
		case '?':
			if (!c || slash || period)
				return FNM_NOMATCH;
			break;
		case '*':
			while (*p == '*')
				p++;
			if (period)
				return FNM_NOMATCH;
			for (;; s++)
			{
				if (gap_match(p, s, start, flags) == 0)
					return 0;
				if (!*s || (*s == '/' && (flags & FNM_PATHNAME)))
					return FNM_NOMATCH;
			}
		case '[':
		{
			const char* q = p;
			const int r = gap_bracket(&q, c, flags);
			if (r < 0)
				goto literal;
			if (!r || !c || slash || period)
				return FNM_NOMATCH;
			p = q - 1; // the p++ below steps past the bracket
			break;
		}
		case '\\':
			if (!(flags & FNM_NOESCAPE) && p[1])
				p++;
			// fallthrough
		default:
		literal:
			if (gap_fold((unsigned char)*p, flags) != gap_fold(c, flags))
				return FNM_NOMATCH;
			break;
		}
		p++;
		s++;
	}
}

int fnmatch(const char* pattern, const char* string, int flags)
{
	return gap_match(pattern, string, string, flags);
}
""")
edit("3rdparty/hidapi/CMakeLists.txt",
     "\t\ttarget_compile_options(hidapi_libusb PRIVATE -U__FreeBSD__ -D__FreeBSD__=11)\n",
     "\t\ttarget_compile_options(hidapi_libusb PRIVATE -U__FreeBSD__ -D__FreeBSD__=11)\n"
     "\t\t# libc functions the PS5 title cannot import (wcsdup, fnmatch)\n"
     "\t\ttarget_sources(hidapi_libusb PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/ps5_libc_gaps.c)\n",
     "ps5_libc_gaps.c)")

# The renderer factory builds the OpenGL backend everywhere but macOS; the PS5 has no OpenGL (no GLEW)
factory = "rpcs3/Emu/RSX/Core/RSXRendererFactory.cpp"
edit(factory, "#ifndef __APPLE__\n", "#if !defined(__APPLE__) && !defined(__PROSPERO__)\n", "__PROSPERO__")
edit(factory, "#if !defined(__APPLE__)\n", "#if !defined(__APPLE__) && !defined(__PROSPERO__)\n", "&& !defined(__PROSPERO__)\n\t\t\treturn", count=2)

# set_target_window() hands keyboard_pad_handler a QWindow; the PS5 has neither (pad_thread.cpp already
# leaves the keyboard handler out there)
edit("rpcs3/Input/pad_thread.cpp",
     "\tm_curwindow = window;\n\n#ifndef ANDROID\n",
     "\tm_curwindow = window;\n\n#if !defined(ANDROID) && !defined(__PROSPERO__)\n",
     "m_curwindow = window;\n\n#if !defined(ANDROID) && !defined(__PROSPERO__)")

# The PS5 title compiles its own list of rpcs3/Input sources. Upstream replaced OpenCV in the PS Move
# tracker with blob_detector.cpp (also holds rgba_draw), and GunCon3 now reads Wiimotes through
# wiimote_handler.cpp and its hid_instance.cpp (hidapi, which rpcs3_emu links now)
edit("rpcs3/ps5/CMakeLists.txt",
     "    ../Input/ps_move_tracker.cpp\n",
     "    ../Input/ps_move_tracker.cpp\n    ../Input/blob_detector.cpp\n    ../Input/hid_instance.cpp\n    ../Input/wiimote_handler.cpp\n",
     "../Input/blob_detector.cpp")

# GSFrameBase now reports the backend its window was made for. The PS5 frame is no window
# (VK_KHR_display), so it serves whichever backend is configured.
edit("rpcs3/ps5/ps5_frame.h",
     "#include \"Emu/RSX/GSFrameBase.h\"\n",
     "#include \"Emu/RSX/GSFrameBase.h\"\n#include \"Emu/RSX/Core/RSXRendererFactory.h\"\n#include \"Emu/system_config.h\"\n",
     "RSXRendererFactory.h")
edit("rpcs3/ps5/ps5_frame.h",
     "\tvoid update_title(double /*fps*/) override {}\n",
     "\tvoid update_title(double /*fps*/) override {}\n\n"
     "\tvideo_renderer renderer() const override { return rsx::resolve_render_backend_type(g_cfg.video.renderer.get()); }\n",
     "video_renderer renderer() const override")

# The RSX thread now picks its backend itself (rsx::make_render_backend, from g_cfg.video.renderer),
# and display_output asks for a fresh frame when the backend changes
main = "rpcs3/ps5/main_ps5.cpp"
edit(main,
     "#include \"Emu/RSX/VK/VKGSRender.h\"\n",
     "#include \"Emu/RSX/VK/VKGSRender.h\"\n#include \"Emu/RSX/Core/RSXRendererFactory.h\"\n",
     "#include \"Emu/RSX/Core/RSXRendererFactory.h\"")
edit(main,
     "\t\t\tswitch (g_cfg.video.renderer.get())\n"
     "\t\t\t{\n"
     "\t\t\tcase video_renderer::null:\n"
     "\t\t\t\tg_fxo->init<rsx::thread, named_thread<NullGSRender>>(ar);\n"
     "\t\t\t\tbreak;\n"
     "\t\t\tdefault:\n"
     "\t\t\t\t// Vulkan through RADV is the only renderer on the PS5\n"
     "\t\t\t\tg_fxo->init<rsx::thread, named_thread<VKGSRender>>(ar);\n"
     "\t\t\t\tbreak;\n"
     "\t\t\t}\n",
     "\t\t\t// Null or Vulkan through RADV, the only renderers on the PS5 (OpenGL falls back to Vulkan)\n"
     "\t\t\trsx::create_render_thread(ar);\n",
     "rsx::create_render_thread(ar);")
edit(main,
     "\t\t\treturn std::make_unique<ps5_frame>(display_width, display_height);\n\t\t};\n",
     "\t\t\treturn std::make_unique<ps5_frame>(display_width, display_height);\n\t\t};\n"
     "\t\tg_emu_callbacks.replace_gs_frame = []() -> std::unique_ptr<GSFrameBase>\n"
     "\t\t{\n"
     "\t\t\treturn std::make_unique<ps5_frame>(display_width, display_height);\n"
     "\t\t};\n",
     "g_emu_callbacks.replace_gs_frame")

# The display engine replaces the renderer whenever its logical ID differs from the configured one,
# and for Vulkan the configured one is the Adapter setting's name. The PS5 has a single GPU, which
# RADV names "PlayStation 5 GPU (RADV NAVI21)" while the PS5 configs say "PS5 GPU (RADV)": the
# renderer was replaced every few frames (the menu flickered, games hung). There it is the GPU in use.
edit("rpcs3/Emu/RSX/VK/VKGSRender.cpp",
     "\t// An empty adapter selects the default GPU, which is the one in use\n",
     "#ifdef __PROSPERO__\n"
     "\t// The PS5 has a single GPU, whatever name the Adapter setting holds\n"
     "\treturn logical_id();\n"
     "#endif\n"
     "\n"
     "\t// An empty adapter selects the default GPU, which is the one in use\n",
     "The PS5 has a single GPU")

# RPCN: the title's libc getaddrinfo fails even for a numeric host ("Failed to getaddrinfo
# 167.235.60.94"), so RPCN never connected. On the PS5 the host (rpcn.yml Host) is taken as an IPv4
# address, parsed here; RPCN does not verify the server certificate, so the address is enough.
rpcn = "rpcs3/Emu/NP/rpcn_client.cpp"
edit(rpcn,
     "\t\t\taddrinfo* addr_info{};\n\n\t\t\tif (getaddrinfo(hostname.c_str(), nullptr, nullptr, &addr_info))\n",
     "#ifdef __PROSPERO__\n"
     "\t\t\t// The PS5 title's libc getaddrinfo fails even for numeric hosts: the RPCN host is an IPv4 address\n"
     "\t\t\tconst auto parse_ipv4 = [](std::string_view text, in_addr& out) -> bool\n"
     "\t\t\t{\n"
     "\t\t\t\tu32 parts[4]{};\n"
     "\t\t\t\tusz part = 0, digits = 0;\n"
     "\t\t\t\tfor (const char c : text)\n"
     "\t\t\t\t{\n"
     "\t\t\t\t\tif (c == '.')\n"
     "\t\t\t\t\t{\n"
     "\t\t\t\t\t\tif (!digits || ++part == 4)\n"
     "\t\t\t\t\t\t\treturn false;\n"
     "\t\t\t\t\t\tdigits = 0;\n"
     "\t\t\t\t\t}\n"
     "\t\t\t\t\telse if (c >= '0' && c <= '9')\n"
     "\t\t\t\t\t{\n"
     "\t\t\t\t\t\tparts[part] = parts[part] * 10 + (c - '0');\n"
     "\t\t\t\t\t\tif (++digits > 3 || parts[part] > 255)\n"
     "\t\t\t\t\t\t\treturn false;\n"
     "\t\t\t\t\t}\n"
     "\t\t\t\t\telse\n"
     "\t\t\t\t\t{\n"
     "\t\t\t\t\t\treturn false;\n"
     "\t\t\t\t\t}\n"
     "\t\t\t\t}\n"
     "\t\t\t\tif (part != 3 || !digits)\n"
     "\t\t\t\t\treturn false;\n"
     "\t\t\t\tout.s_addr = std::bit_cast<u32, be_t<u32>>((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]);\n"
     "\t\t\t\treturn true;\n"
     "\t\t\t};\n"
     "\n"
     "\t\t\tif (!parse_ipv4(hostname, addr_rpcn.sin_addr))\n"
     "\t\t\t{\n"
     "\t\t\t\trpcn_log.error(\"connect: on the PS5 the RPCN host must be an IPv4 address (rpcn.yml Host), not %s\", host);\n"
     "\t\t\t\tstate = rpcn_state::failure_resolve;\n"
     "\t\t\t\treturn false;\n"
     "\t\t\t}\n"
     "#else\n"
     "\t\t\taddrinfo* addr_info{};\n\n\t\t\tif (getaddrinfo(hostname.c_str(), nullptr, nullptr, &addr_info))\n",
     "RPCN host is an IPv4 address")
edit(rpcn,
     "\t\t\tmemcpy(&addr_rpcn_udp_ipv4, &addr_rpcn, sizeof(addr_rpcn_udp_ipv4));\n",
     "#endif\n\n"
     "\t\t\tmemcpy(&addr_rpcn_udp_ipv4, &addr_rpcn, sizeof(addr_rpcn_udp_ipv4));\n",
     "#endif\n\n\t\t\tmemcpy(&addr_rpcn_udp_ipv4")
