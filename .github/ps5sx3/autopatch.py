#!/usr/bin/env python3
"""Applies the The Bigs 2 crowd fix (RPCS3 issue #17741, digant73's commit 5cdab28) to a PS5SX3
eboot.bin, which ships without source code.

The fix lives in rsx::interleaved_range_info::calculate_required_range(): the fallback that walks the
index array must start at draw_clause.min_index() and cover get_elements_count() elements (and, for
immediate-mode draws, the push buffer's own size) instead of [first, first + count).

How a build is recognised: the function is found through its name, which the binary keeps for
ensure()/get_address() messages, then compared instruction by instruction with a reference build.
Only addresses that legitimately move between builds may differ. If anything else differs the build is
refused and nothing is written, so an unknown build is never half-patched.

There are two references, because the developer changed compiler settings in TB24:
  reference_tb20.json  TB18.7 .. TB22.1: frame pointer kept, locals addressed from rbp
  reference_tb24.json  TB24.1 onwards:   frame pointer omitted, locals addressed from rsp, so the
                                         registers and stack slots of the patch are different
A build is patched with the first reference it matches exactly.

Usage: autopatch.py <eboot.bin> <out.bin> [--report report.json]
Exit codes: 0 patched, 3 the fix seems to be in the build already (nothing written), 2 refused.
"""
import hashlib
import json
import os
import re
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_REG_RBP, X86_REG_RIP, X86_REG_RSP
from keystone import Ks, KS_ARCH_X86, KS_MODE_64

HERE = os.path.dirname(os.path.abspath(__file__))
CALC_NAME = b"std::pair<u32, u32> rsx::interleaved_range_info::calculate_required_range(u32, u32)\0"
RAW_NAME = b"std::span<const std::byte> rsx::draw_command_processor::get_raw_index_array(const draw_clause &) const\0"


class Refused(Exception):
    pass


class AlreadyFixed(Exception):
    pass


class FakeSelf:
    """A fake-signed SELF: plain ELF segments behind a small header."""

    def __init__(self, data):
        self.data = data
        if struct.unpack_from("<I", data, 0)[0] != 0x1D3D154F:
            raise Refused("not a SELF file")
        nseg = struct.unpack_from("<H", data, 0x18)[0]
        entries = [struct.unpack_from("<QQQQ", data, 0x20 + i * 32) for i in range(nseg)]
        elf = 0x20 + nseg * 32
        if data[elf:elf + 4] != b"\x7fELF":
            raise Refused("no ELF header inside the SELF")
        phoff = struct.unpack_from("<Q", data, elf + 0x20)[0]
        phentsize, phnum = struct.unpack_from("<HH", data, elf + 0x36)
        phdrs = [struct.unpack_from("<IIQQQQQQ", data, elf + phoff + i * phentsize) for i in range(phnum)]
        self.maps = []   # (vaddr, size, self offset, executable)
        self.text = None
        for i, (flags, off, csize, usize) in enumerate(entries):
            if not flags & 0x800:
                continue  # a digest table, not segment data
            p_type, p_flags, _, p_vaddr, _, p_filesz, _, _ = phdrs[flags >> 20]
            if csize != usize or usize != p_filesz:
                raise Refused("a SELF segment is compressed")
            if p_type != 1:
                continue
            self.maps.append((p_vaddr, usize, off, bool(p_flags & 1)))
            if p_flags & 1:
                if self.text is not None:
                    raise Refused("more than one code segment")
                self.text = (p_vaddr, usize, off)
                # The digest table of this segment is the entry before it. A fake-signed SELF leaves
                # it zeroed; real digests would have to be recomputed, which this tool does not do.
                dflags, doff, dsize, _ = entries[i - 1]
                if i == 0 or dflags & 0x800 or dflags >> 20 != i or any(data[doff:doff + dsize]):
                    raise Refused("the code segment has block digests")
        if self.text is None:
            raise Refused("no code segment")

    def off(self, va, size=1):
        for vaddr, seg_size, off, _ in self.maps:
            if vaddr <= va and va + size <= vaddr + seg_size:
                return off + va - vaddr
        raise Refused(f"address {va:#x} is not in the file")

    def va(self, off):
        for vaddr, seg_size, seg_off, _ in self.maps:
            if seg_off <= off < seg_off + seg_size:
                return vaddr + off - seg_off
        return None

    def read(self, va, size):
        o = self.off(va, size)
        return bytes(self.data[o:o + size])


def source_location_xrefs(sf, name):
    """Code addresses of 'lea reg, [rip + X]' where X is a std::source_location naming the function."""
    hits = [m.start() for m in re.finditer(re.escape(name), sf.data)]
    if len(hits) != 1:
        raise Refused(f"function name found {len(hits)} times: {name[:60].decode()}")
    name_va = sf.va(hits[0])
    if name_va is None:
        raise Refused("function name is outside the loaded segments")
    structs = set()
    for m in re.finditer(re.escape(struct.pack("<Q", name_va)), sf.data):
        if m.start() < 16:
            continue
        r_offset, r_info = struct.unpack_from("<QQ", sf.data, m.start() - 16)
        if r_info == 8:  # R_X86_64_RELATIVE filling source_location::function
            structs.add(r_offset - 8)
    text_va, text_size, text_off = sf.text
    text = sf.data[text_off:text_off + text_size]
    xrefs = []
    for m in re.finditer(rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]", text, re.S):
        p = m.start()
        if p + 7 > len(text):
            continue
        target = text_va + p + 7 + struct.unpack_from("<i", text, p + 3)[0]
        if target in structs:
            xrefs.append(text_va + p)
    return sorted(structs), sorted(xrefs)


MD = Cs(CS_ARCH_X86, CS_MODE_64)
MD.detail = True


def variable_fields(insn):
    """(offset, size, kind, value) of the parts of an instruction that may move between builds."""
    out = []
    enc = insn.encoding
    for op in insn.operands:
        if op.type == X86_OP_MEM and enc.disp_size == 4:
            if op.mem.base == X86_REG_RIP:
                out.append((enc.disp_offset, 4, "sym", insn.address + insn.size + op.mem.disp))
            elif op.mem.base not in (X86_REG_RBP, X86_REG_RSP):
                out.append((enc.disp_offset, 4, "field", op.mem.disp & 0xFFFFFFFF))
        elif op.type == X86_OP_IMM and insn.mnemonic == "call" and enc.imm_size == 4:
            out.append((enc.imm_offset, 4, "sym", op.imm))
    return out


def compare(ref_code, ref_va, tgt_code, tgt_va, what):
    """Lockstep comparison. Returns ({ref address: target address}, {ref field offset: target one})."""
    syms, fields = {}, {}
    ref_insns = list(MD.disasm(ref_code, ref_va))
    tgt_insns = list(MD.disasm(tgt_code, tgt_va))
    if sum(i.size for i in ref_insns) != len(ref_code):
        raise Refused(f"{what}: the reference does not disassemble cleanly")
    if len(tgt_insns) < len(ref_insns):
        raise Refused(f"{what}: the code is shaped differently (it ends early)")
    for r, t in zip(ref_insns, tgt_insns):
        where = f"{what} at {t.address:#x} (reference {r.address:#x})"
        if r.size != t.size or r.mnemonic != t.mnemonic:
            raise Refused(f"{where}: '{t.mnemonic} {t.op_str}' instead of '{r.mnemonic} {r.op_str}'")
        rb, tb = bytearray(r.bytes), bytearray(t.bytes)
        rf, tf = variable_fields(r), variable_fields(t)
        if [f[:3] for f in rf] != [f[:3] for f in tf]:
            raise Refused(f"{where}: operands differ: '{t.op_str}' instead of '{r.op_str}'")
        for (o, n, kind, rv), (_, _, _, tv) in zip(rf, tf):
            rb[o:o + n] = tb[o:o + n] = b"\0" * n
            table = syms if kind == "sym" else fields
            if table.setdefault(rv, tv) != tv:
                raise Refused(f"{where}: {kind} {rv:#x} maps to both {table[rv]:#x} and {tv:#x}")
        if rb != tb:
            raise Refused(f"{where}: '{t.mnemonic} {t.op_str}' instead of '{r.mnemonic} {r.op_str}'")
    return syms, fields


PLACEHOLDERS = {"FXO": 0x11111111, "SRCLOC": 0x22222222, "SUDO": 0x33333333}

# TB20 style: frame pointer in rbp, RSX thread in r11, rdi saved in r15.
PATCH_A = """
    mov r14, rdx
    mov r15, rdi
    mov rbx, rsi
    xor ecx, ecx
    mov dword ptr [rbp - 0x2c], ecx
    mov dword ptr [rbp - 0x34], ecx
    lea rcx, [rbp - 0x34]
    mov qword ptr [rbp - 0x70], rcx
    lea rcx, [rbp - 0x38]
    mov qword ptr [rbp - 0x68], rcx
    lea rcx, [rbp - 0x30]
    mov qword ptr [rbp - 0x60], rcx
    lea rcx, [rbp - 0xb0]
    mov qword ptr [rbp - 0x58], rcx
    lea rcx, [rbp - 0x2c]
    mov qword ptr [rbp - 0x50], rcx
    lea r10, [rip + 0x11111111]
    mov rsi, qword ptr [r10 + r9 + {PB_DATA}]
    mov ecx, dword ptr [r10 + r9 + {PB_SIZE}]
    test ecx, ecx
    je array_case
    mov dword ptr [rbp - 0x38], ecx
    jmp call_u32
array_case:
    movzx r12d, r8b
    mov ecx, dword ptr [r11 + {RANGE_INDEX}]
    cmp ecx, dword ptr [r11 + {RANGE_COUNT}]
    jae {FAIL}
    mov rdx, qword ptr [r11 + {RANGE_DATA}]
    lea rcx, [rcx + rcx*2]
    mov r13d, dword ptr [rdx + rcx*4 + 4]
    mov ecx, dword ptr [rdx + rcx*4 + 8]
    mov dword ptr [rbp - 0x38], ecx
    mov edi, 0x1fffffff
    and edi, dword ptr [r11 + {INDEX_ADDR}]
    and eax, 0xf
    mov esi, eax
    xor edx, edx
    lea rcx, [rip + 0x22222222]
    call {GET_ADDRESS}
    lea rcx, [rip + 0x33333333]
    mov rsi, qword ptr [rcx]
    test r12d, r12d
    je {B_START}
    and eax, 0xfffffffc
    add rsi, rax
    lea rsi, [rsi + r13*4]
call_u32:
    lea rdi, [rbp - 0x70]
    call {LAMBDA_U32}
    jmp {JOIN}
"""

PATCH_B = """
    and eax, 0xfffffffe
    add rsi, rax
    lea rsi, [rsi + r13*2]
    lea rdi, [rbp - 0x70]
    call {LAMBDA_U16}
    jmp {JOIN}
"""


# TB24 style: no frame pointer (locals from rsp), RSX thread in r15, rdi saved in r12, rbp and r13 free.
# The same code as PATCH_A with these slots: [rbp-0x34]->[rsp+0x10] first, [rbp-0x38]->[rsp+0x14] count,
# [rbp-0x30]->[rsp+0xc], [rbp-0xb0]->[rsp+0x50], [rbp-0x2c]->[rsp+0x8] max, lambda captures at [rsp+0x28].
PATCH_A_NOFP = """
    mov r14, rdx
    mov r12, rdi
    mov rbx, rsi
    xor ecx, ecx
    mov dword ptr [rsp + 0x8], ecx
    mov dword ptr [rsp + 0x10], ecx
    lea rcx, [rsp + 0x10]
    mov qword ptr [rsp + 0x28], rcx
    lea rcx, [rsp + 0x14]
    mov qword ptr [rsp + 0x30], rcx
    lea rcx, [rsp + 0xc]
    mov qword ptr [rsp + 0x38], rcx
    lea rcx, [rsp + 0x50]
    mov qword ptr [rsp + 0x40], rcx
    lea rcx, [rsp + 0x8]
    mov qword ptr [rsp + 0x48], rcx
    lea r10, [rip + 0x11111111]
    mov rsi, qword ptr [r10 + r9 + {PB_DATA}]
    mov ecx, dword ptr [r10 + r9 + {PB_SIZE}]
    test ecx, ecx
    je array_case
    mov dword ptr [rsp + 0x14], ecx
    jmp call_u32
array_case:
    movzx ebp, r8b
    mov ecx, dword ptr [r15 + {RANGE_INDEX}]
    cmp ecx, dword ptr [r15 + {RANGE_COUNT}]
    jae {FAIL}
    mov rdx, qword ptr [r15 + {RANGE_DATA}]
    lea rcx, [rcx + rcx*2]
    mov r13d, dword ptr [rdx + rcx*4 + 4]
    mov ecx, dword ptr [rdx + rcx*4 + 8]
    mov dword ptr [rsp + 0x14], ecx
    mov edi, 0x1fffffff
    and edi, dword ptr [r15 + {INDEX_ADDR}]
    and eax, 0xf
    mov esi, eax
    xor edx, edx
    lea rcx, [rip + 0x22222222]
    call {GET_ADDRESS}
    lea rcx, [rip + 0x33333333]
    mov rsi, qword ptr [rcx]
    test ebp, ebp
    je {B_START}
    and eax, 0xfffffffc
    add rsi, rax
    lea rsi, [rsi + r13*4]
call_u32:
    lea rdi, [rsp + 0x28]
    call {LAMBDA_U32}
    jmp {JOIN}
"""

PATCH_B_NOFP = """
    and eax, 0xfffffffe
    add rsi, rax
    lea rsi, [rsi + r13*2]
    lea rdi, [rsp + 0x28]
    call {LAMBDA_U16}
    jmp {JOIN}
"""

TEMPLATES = {"rbp": (PATCH_A, PATCH_B), "rsp": (PATCH_A_NOFP, PATCH_B_NOFP)}
REFERENCES = ("reference_tb20.json", "reference_tb24.json")


def assemble(template, v, start, end):
    code = bytearray(Ks(KS_ARCH_X86, KS_MODE_64).asm(template.format(**v), start)[0])
    size = len(code)
    if size > end - start:
        raise Refused(f"the replacement code ({size} bytes) does not fit in {end - start}")
    plain = Cs(CS_ARCH_X86, CS_MODE_64)
    for insn in plain.disasm(bytes(code), start):  # resolve the rip-relative placeholders
        if insn.size < 7 or "rip" not in insn.op_str:
            continue
        at = insn.address - start + insn.size - 4
        disp = struct.unpack_from("<i", code, at)[0]
        for name, marker in PLACEHOLDERS.items():
            if disp == marker:
                struct.pack_into("<i", code, at, v[name] - (insn.address + insn.size))
    for marker in PLACEHOLDERS.values():
        if struct.pack("<I", marker) in code:
            raise Refused("a placeholder was left in the replacement code")
    return bytes(code) + b"\xcc" * (end - start - size)


def autopatch(data):
    """Returns (patched bytes, report). Raises Refused or AlreadyFixed."""
    sf = FakeSelf(bytearray(data))

    structs, xrefs = source_location_xrefs(sf, CALC_NAME)
    if len(structs) == 2:
        # Upstream's fix has one get_address() call left in the function instead of two.
        raise AlreadyFixed("calculate_required_range has the shape of the fixed source")
    if len(structs) != 3 or len(xrefs) != 3:
        raise Refused(f"calculate_required_range: {len(structs)} source locations and {len(xrefs)} "
                      "references instead of 3 and 3")
    _, raw_xrefs = source_location_xrefs(sf, RAW_NAME)
    if not raw_xrefs:
        raise Refused("get_raw_index_array: no reference found")

    reasons = []
    for name in REFERENCES:
        ref = json.load(open(os.path.join(HERE, name)))
        try:
            patches, report = patch_with(ref, sf, structs, xrefs, raw_xrefs)
        except Refused as e:
            reasons.append(f"{ref['build']}: {e}")
            continue
        out = bytearray(data)
        for va, code in patches:
            o = sf.off(va, len(code))
            out[o:o + len(code)] = code
        report = {
            "status": "patched", "reference": ref["build"],
            "input_sha256": hashlib.sha256(data).hexdigest(),
            "output_sha256": hashlib.sha256(out).hexdigest(),
            **report,
            "changed_bytes": sum(1 for x, y in zip(data, out) if x != y),
        }
        return bytes(out), report
    raise Refused("this build matches no reference; " + " | ".join(reasons))


def patch_with(ref, sf, structs, xrefs, raw_xrefs):
    """The patches for one reference: [(address, code)], report. Raises Refused if it does not match."""
    patch_a, patch_b = TEMPLATES[ref.get("frame", "rbp")]
    calc_ref_va, calc_ref = ref["calc"]["va"], bytes.fromhex(ref["calc"]["code"])
    calc_va = xrefs[0] - (ref["calc"]["first_xref"] - calc_ref_va)
    syms, fields = compare(calc_ref, calc_ref_va, sf.read(calc_va, len(calc_ref)), calc_va,
                           "calculate_required_range")

    raw_ref_va, raw_ref = ref["raw"]["va"], bytes.fromhex(ref["raw"]["code"])
    raw_va = raw_xrefs[0] - (ref["raw"]["first_xref"] - raw_ref_va)
    _, raw_fields = compare(raw_ref, raw_ref_va, sf.read(raw_va, len(raw_ref)), raw_va,
                            "get_raw_index_array")

    # draw_clause layout (ranges data +0x40, count +0x48, current range +0x90, command +0xfc) is read
    # from get_raw_index_array: the small offsets are part of the exact comparison above, these two are
    # wide enough to be 'fields', so they are pinned here.
    for off in (0x90, 0xFC):
        if raw_fields.get(off) != off:
            raise Refused(f"draw_clause field {off:#x} moved to {raw_fields.get(off)}")
    r = ref["calc"]["fields"]   # field offsets as seen in the reference build
    command = fields.get(r["command"])
    if command is None or fields.get(r["immediate"]) != command + 4:
        raise Refused("current_draw_clause.command / is_immediate_draw are not where expected")
    pb_data = fields.get(r["push_buffer_data"])
    if pb_data is None or fields.get(r["push_buffer_size"]) != pb_data + 8:
        raise Refused("element_push_buffer is not where expected")
    for reg in r["registers"]:  # RSX register slots: fixed by the PS3's GPU, not by the build
        if fields.get(reg) != reg:
            raise Refused(f"RSX register offset {reg:#x} moved to {fields.get(reg)}")

    delta = calc_va - calc_ref_va
    a = ref["calc"]["addresses"]
    lambda_u32, lambda_u16 = a["lambda_u32"] + delta, a["lambda_u16"] + delta
    if syms.get(a["lambda_u32"]) != lambda_u32 or syms.get(a["lambda_u16"]) != lambda_u16:
        raise Refused("the two index scanning lambdas are not where expected")
    clause = command - 0xFC
    v = {
        "FXO": syms[a["fxo"]], "SRCLOC": syms[a["srcloc"]], "SUDO": syms[a["sudo"]],
        "GET_ADDRESS": syms[a["get_address"]], "LAMBDA_U32": lambda_u32, "LAMBDA_U16": lambda_u16,
        "JOIN": a["join"] + delta, "FAIL": a["fail"] + delta, "B_START": a["b_start"] + delta,
        "PB_DATA": pb_data, "PB_SIZE": pb_data + 8, "INDEX_ADDR": r["index_array_address"],
        "RANGE_DATA": clause + 0x40, "RANGE_COUNT": clause + 0x48, "RANGE_INDEX": clause + 0x90,
    }
    if v["SRCLOC"] != structs[0]:
        raise Refused("get_address's source location is not the first one of the function")
    start, join = a["start"] + delta, v["JOIN"]
    b_start, b_end = v["B_START"], a["b_end"] + delta
    patches = [(start, assemble(patch_a, v, start, join)), (b_start, assemble(patch_b, v, b_start, b_end))]
    return patches, {"function": hex(calc_va), "moved_by": delta, "values": {k: hex(x) for k, x in v.items()}}


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    report_path = argv[argv.index("--report") + 1] if "--report" in argv else None
    data = open(argv[1], "rb").read()
    code = 0
    try:
        out, report = autopatch(data)
        open(argv[2], "wb").write(out)
    except AlreadyFixed as e:
        report, code = {"status": "already_fixed", "reason": str(e)}, 3
    except Refused as e:
        report, code = {"status": "refused", "reason": str(e)}, 2
    print(json.dumps(report, indent=1))
    if report_path:
        json.dump(report, open(report_path, "w"), indent=1)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
