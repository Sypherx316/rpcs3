# Applies the The Bigs 2 fix (RPCS3#17741, digant73 5cdab28) to rpcs3/Emu/RSX/RSXThread.cpp.
# Works on both the pre-refactor RSX code (rsx::method_registers) and the RSXContext one
# (REGS(ctx), get_address(ctx, ...)) that upstream introduced in October 2026.
import sys

path = sys.argv[1]
with open(path, encoding="utf-8", newline="") as f:
    text = f.read()

# Windows checkouts may have CRLF line endings; work on LF and write the file back as it was
crlf = "\r\n" in text
text = text.replace("\r\n", "\n")

if "_index < element_count" in text:
    print("fix already applied")
    sys.exit(0)


def replace_once(old, new):
    global text
    assert text.count(old) == 1, f"expected exactly one match for: {old[:70]}"
    text = text.replace(old, new)


replace_once("(const std::byte* ptr, T)", "(const std::byte* ptr, u32 element_count, T)")
replace_once("for (u32 _index = first; _index < first + count; _index++)",
             "for (u32 _index = 0; _index < element_count; _index++)")

# The index pointer selection right after re_evaluate, up to the "break;" that ends the loop body
loop = text.index("for (u32 _index = 0; _index < element_count; _index++)")
start = text.index("const auto element_push_buffer = render->draw_processor()->element_push_buffer();", loop)
start = text.rindex("\n", 0, start) + 1
end = text.index("\n\n\t\t\tbreak;\n", start)
old_block = text[start:end]
assert old_block.count("re_evaluate(") == 4, "unexpected index pointer selection block"

regs = "REGS(ctx)" if "REGS(ctx)->index_array_address()" in old_block else "rsx::method_registers"
get_address = "get_address(ctx, " if "get_address(ctx, " in old_block else "get_address("
indent = old_block[:len(old_block) - len(old_block.lstrip("\t"))]

rows = [
    (0, "// NOTE: The index array is addressed by element position within the draw range."),
    (0, "// [first, first + count) are vertex index values and have nothing to do with element positions. See get_raw_index_array()."),
    (0, "const auto element_push_buffer = render->draw_processor()->element_push_buffer();"),
    (0, "if (!element_push_buffer.empty()) [[unlikely]]"), (0, "{"),
    (1, "// Indices provided via immediate mode, always u32 and never offset into the index array"),
    (1, "re_evaluate(reinterpret_cast<const std::byte*>(element_push_buffer.data()), ::size32(element_push_buffer), u32{});"),
    (0, "}"), (0, "else"), (0, "{"),
    (1, f"const auto& draw_clause = {regs}.current_draw_clause;" if regs == "rsx::method_registers"
        else f"const auto& draw_clause = {regs}->current_draw_clause;"),
    (-1, ""),
    (1, "// Force aligned indices as realhw"),
    (1, f"const u32 address = (0 - index_size) & {get_address}{regs}{'->' if regs != 'rsx::method_registers' else '.'}index_array_address(), "
        f"{regs}{'->' if regs != 'rsx::method_registers' else '.'}index_array_location());"),
    (1, "const std::byte* const ptr = vm::get_super_ptr<std::byte>(address) + draw_clause.min_index() * index_size;"),
    (1, "const u32 element_count = draw_clause.get_elements_count();"),
    (-1, ""),
    (1, "if (index_size == 4)"), (1, "{"),
    (2, "re_evaluate(ptr, element_count, u32{});"),
    (1, "}"), (1, "else"), (1, "{"),
    (2, "re_evaluate(ptr, element_count, u16{});"),
    (1, "}"),
    (0, "}"),
]
new_block = "\n".join("" if n < 0 else indent + "\t" * n + s for n, s in rows)
text = text[:start] + new_block + text[end:]

with open(path, "w", encoding="utf-8", newline="") as f:
    f.write(text.replace("\n", "\r\n") if crlf else text)
print(f"fix applied ({regs})")
