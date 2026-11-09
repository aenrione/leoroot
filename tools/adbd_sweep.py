#!/usr/bin/env python3
"""Static sweeps over the stripped ARM Thumb-2 adbd from the TA-1324 ramdisk.

Hopper's xref pass can miss call sites whose containing function got truncated
during analysis, so this re-derives the facts straight from the file bytes.

Sweeps:
  bl       every Thumb-2 BL/BLX in the executable segment whose target is
           --target (default 0x2a1a8 = the setuid32 stub); prints each site.
  svc      every `svc` with a nearby `mov[s] r7,#imm` immediate, i.e. inline
           syscalls; used to catch a privilege drop that bypasses the stub.
  lit      every 4-byte little-endian occurrence of the given constants, to
           find function pointers parked in a dispatch table.

Addresses in and out are VIRTUAL (Hopper numbering). Mapping comes from the
ELF program headers, not hardcoded.

Usage:
  python3 -I tools/adbd_sweep.py <elf> bl  [--target 0x2a1a8]
  python3 -I tools/adbd_sweep.py <elf> svc
  python3 -I tools/adbd_sweep.py <elf> lit 0x91cc 0x92b8 0x940c 0xb0d4 0xde00
"""
import struct
import sys


def load_segments(blob):
    """Return [(fileoff, vaddr, filesz, flags)] from the ELF32 LE program headers."""
    assert blob[:4] == b"\x7fELF", "not an ELF"
    assert blob[4] == 1, "expected ELF32"
    e_phoff, = struct.unpack_from("<I", blob, 0x1C)
    e_phentsize, e_phnum = struct.unpack_from("<HH", blob, 0x2A)
    segs = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, p_offset, p_vaddr, _p_paddr, p_filesz, _p_memsz, p_flags, _a = \
            struct.unpack_from("<8I", blob, off)
        if p_type == 1:  # PT_LOAD
            segs.append((p_offset, p_vaddr, p_filesz, p_flags))
    return segs


def v2f(segs, vaddr):
    for p_offset, p_vaddr, p_filesz, _ in segs:
        if p_vaddr <= vaddr < p_vaddr + p_filesz:
            return p_offset + (vaddr - p_vaddr)
    return None


def f2v(segs, fileoff):
    for p_offset, p_vaddr, p_filesz, _ in segs:
        if p_offset <= fileoff < p_offset + p_filesz:
            return p_vaddr + (fileoff - p_offset)
    return None


def exec_segment(segs):
    """The PT_LOAD carrying code (PF_X == 1)."""
    for s in segs:
        if s[3] & 1:
            return s
    return segs[0]


# The 8 setuid call sites enumerated via Hopper xrefs in the prior session.
# sweep_bl must reproduce every one of these or the decoder is wrong.
KNOWN_SETUID_SITES = [0x91E4, 0x92BE, 0x9454, 0xB0EC, 0xDEB0, 0xDED8, 0x13FB2, 0x233F4]


def decode_branch(hw1, hw2, site):
    """Thumb-2 BL / BLX(imm) target per ARM ARM A8.8.25-26, or None.

    hw1 = 11110 S imm10
    hw2 = 11 J1 X J2 imm11   where X=1 selects BL, X=0 selects BLX(imm)
    BL keeps site+4 as the base; BLX(imm) aligns it down to 4.
    """
    if (hw1 & 0xF800) != 0xF000:
        return None
    if (hw2 & 0xC000) != 0xC000:  # bits[15:14] must be 0b11
        return None
    is_bl = bool(hw2 & 0x1000)
    s = (hw1 >> 10) & 1
    j1 = (hw2 >> 13) & 1
    j2 = (hw2 >> 11) & 1
    i1 = 1 - (j1 ^ s)
    i2 = 1 - (j2 ^ s)
    imm10 = hw1 & 0x3FF
    imm11 = hw2 & 0x7FF
    if not is_bl and (imm11 & 1):  # BLX(imm) H bit must be 0
        return None
    off = (s << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
    if s:  # sign-extend the 25-bit displacement
        off -= 1 << 25
    base = (site + 4) if is_bl else ((site + 4) & ~3)
    return base + off, ("bl" if is_bl else "blx")


def sweep_bl(blob, segs, target):
    p_offset, _p_vaddr, p_filesz, _ = exec_segment(segs)
    hits = []
    for off in range(p_offset, p_offset + p_filesz - 3, 2):
        hw1, hw2 = struct.unpack_from("<HH", blob, off)
        site = f2v(segs, off)
        got = decode_branch(hw1, hw2, site)
        if got and got[0] == target:
            hits.append((site, off, hw1, hw2, got[1]))
    return hits


def sweep_svc(blob, segs, lookback=8):
    """Find `svc` (0xDF00-0xDFFF) and the most recent mov[s] r7,#imm before it."""
    p_offset, p_vaddr, p_filesz, _ = exec_segment(segs)
    out = []
    for off in range(p_offset, p_offset + p_filesz - 1, 2):
        hw, = struct.unpack_from("<H", blob, off)
        if 0xDF00 <= hw <= 0xDFFF:
            nr = None
            for back in range(1, lookback + 1):
                b = off - back * 2
                if b < p_offset:
                    break
                prev, = struct.unpack_from("<H", blob, b)
                if (prev & 0xFF00) == 0x2700:  # movs r7, #imm8
                    nr = prev & 0xFF
                    break
                if prev == 0xF04F:  # mov.w r7,#imm start; read next hw
                    nxt, = struct.unpack_from("<H", blob, b + 2)
                    if (nxt & 0x0F00) == 0x0700:
                        nr = nxt & 0xFF
                        break
            out.append((f2v(segs, off), hw & 0xFF, nr))
    return out


def arm_expand_imm(w):
    """ARM data-processing modified immediate: ror(imm8, 2*rotate)."""
    imm8 = w & 0xFF
    rot = ((w >> 8) & 0xF) * 2
    return ((imm8 >> rot) | (imm8 << (32 - rot))) & 0xFFFFFFFF if rot else imm8


def sweep_armsvc(blob, segs, lookback=4):
    """Enumerate bionic ARM syscall stubs: `svc 0` plus the NR loaded into r7.

    The 8 getuid call sites all decode as BLX, i.e. they exchange to ARM state,
    so the stubs are ARM-mode and a Thumb 0xDF00 scan finds nothing real.
    Canonical bionic stub shape:
        e1a0c007  mov r12, r7
        e3a070xx  mov r7, #NR          (or e59f7xxx: ldr r7,[pc,#imm] for big NR)
        ef000000  svc 0
        e1a0700c  mov r7, r12
    Only `svc 0` exactly (0xEF000000) is treated as a syscall, which keeps
    Thumb code misread as ARM words out of the results.
    """
    p_offset, _p_vaddr, p_filesz, _ = exec_segment(segs)
    out = []
    limit = p_offset + p_filesz
    for off in range(p_offset, limit - 3, 4):
        w, = struct.unpack_from("<I", blob, off)
        if w != 0xEF000000:  # svc 0
            continue
        nr = None
        how = None
        start = off
        for back in range(1, lookback + 1):
            b = off - back * 4
            if b < p_offset:
                break
            p, = struct.unpack_from("<I", blob, b)
            if (p & 0x0FFFF000) == 0x03A07000:  # mov r7, #imm
                nr, how, start = arm_expand_imm(p), "mov", b
                break
            if (p & 0x0FFFF000) == 0x059F7000:  # ldr r7, [pc, #imm12]
                lit = b + 8 + (p & 0xFFF)
                lf = v2f(segs, f2v(segs, lit))
                if lf is not None and lf + 4 <= len(blob):
                    nr, = struct.unpack_from("<I", blob, lf)
                how, start = "ldr", b
                break
        out.append((f2v(segs, start), f2v(segs, off), nr, how))
    return out


def sweep_lit(blob, segs, consts):
    hits = {}
    for c in consts:
        pat = struct.pack("<I", c)
        found = []
        start = 0
        while True:
            i = blob.find(pat, start)
            if i < 0:
                break
            found.append((i, f2v(segs, i)))
            start = i + 1
        # Thumb pointers are often stored with bit0 set
        pat_t = struct.pack("<I", c | 1)
        start = 0
        while True:
            i = blob.find(pat_t, start)
            if i < 0:
                break
            found.append((i, f2v(segs, i), "thumb|1"))
            start = i + 1
        hits[c] = found
    return hits


def main():
    path, mode = sys.argv[1], sys.argv[2]
    blob = open(path, "rb").read()
    segs = load_segments(blob)

    print(f"# {path}  ({len(blob)} bytes)")
    for p_offset, p_vaddr, p_filesz, flags in segs:
        print(f"# PT_LOAD file 0x{p_offset:x} -> vaddr 0x{p_vaddr:x} "
              f"filesz 0x{p_filesz:x} flags {flags} "
              f"(vaddr-0x{p_vaddr - p_offset:x} = file)")
    print()

    if mode == "bl":
        target = 0x2A1A8
        if "--target" in sys.argv:
            target = int(sys.argv[sys.argv.index("--target") + 1], 16)
        hits = sweep_bl(blob, segs, target)
        found = {v for v, _f, _a, _b, _k in hits}
        print(f"BL/BLX sites targeting 0x{target:x}: {len(hits)}")
        for v, f, hw1, hw2, kind in hits:
            mark = "known" if v in KNOWN_SETUID_SITES else "*** NEW ***"
            print(f"  vaddr 0x{v:<8x} file 0x{f:<8x} {kind} bytes "
                  f"{hw1 & 0xff:02x} {hw1 >> 8:02x} {hw2 & 0xff:02x} {hw2 >> 8:02x}"
                  f"   {mark}")
        if target == 0x2A1A8:
            missing = [a for a in KNOWN_SETUID_SITES if a not in found]
            print()
            print("decoder self-check against the 8 Hopper-enumerated sites:")
            print(f"  reproduced : {len(KNOWN_SETUID_SITES) - len(missing)}/8")
            if missing:
                print("  MISSING    : " + ", ".join(f"0x{a:x}" for a in missing)
                      + "   <-- decoder is wrong, do not trust this sweep")
            new = sorted(found - set(KNOWN_SETUID_SITES))
            print("  new sites  : "
                  + (", ".join(f"0x{a:x}" for a in new) if new else "none"))

    elif mode == "svc":
        out = sweep_svc(blob, segs)
        print(f"svc instructions: {len(out)}")
        interesting = {199, 200, 201, 214, 47, 20, 23, 24, 46, 49, 50, 185, 220}
        for v, imm, nr in out:
            tag = ""
            if nr in interesting:
                tag = f"   <== NR {nr}"
            print(f"  vaddr 0x{v:<8x} svc #{imm}  r7={nr}{tag}")

    elif mode == "armsvc":
        # ARM EABI syscall numbers worth flagging in a privilege-drop hunt.
        names = {
            20: "getpid", 23: "setuid(16)", 46: "setgid(16)", 47: "getgid(16)",
            49: "geteuid(16)", 50: "getegid(16)", 70: "setreuid(16)",
            71: "setregid(16)", 81: "setgroups(16)", 97: "setpriority",
            164: "setresuid(16)", 170: "setresgid(16)", 172: "prctl",
            185: "capget", 186: "capset", 199: "getuid32", 200: "getgid32",
            201: "geteuid32", 202: "getegid32", 203: "setreuid32",
            204: "setregid32", 205: "getgroups32", 206: "setgroups32",
            208: "setresuid32", 210: "setresgid32", 211: "getresgid32",
            213: "setuid32", 214: "setgid32", 215: "setfsuid32",
            216: "setfsgid32", 220: "madvise",
        }
        PRIV = {23, 46, 70, 71, 81, 164, 170, 172, 186, 203, 204, 206,
                208, 210, 213, 214, 215, 216}
        out = sweep_armsvc(blob, segs)
        print(f"bionic ARM syscall stubs (svc 0): {len(out)}")
        priv = []
        for start, svc, nr, how in out:
            nm = names.get(nr, "")
            print(f"  stub 0x{start:<7x} svc 0x{svc:<7x} NR {nr}"
                  f"  [{how}]  {nm}")
            if nr in PRIV:
                priv.append((start, nr, nm))
        print()
        print("=== privilege-changing stubs (the drop must call one) ===")
        for s, nr, nm in priv:
            print(f"  stub 0x{s:x}  NR {nr}  {nm}")
        if not priv:
            print("  none")

    elif mode == "lit":
        consts = [int(x, 16) for x in sys.argv[3:]]
        hits = sweep_lit(blob, segs, consts)
        for c, found in hits.items():
            print(f"0x{c:x}: {len(found)} occurrence(s)")
            for item in found:
                f = item[0]
                v = item[1]
                note = f"  {item[2]}" if len(item) > 2 else ""
                vs = f"0x{v:x}" if v is not None else "(unmapped)"
                print(f"  file 0x{f:<8x} vaddr {vs}{note}")
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
