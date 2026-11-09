#!/usr/bin/env python3
"""Neutralise the start-time privilege drop in the TA-1324 adbd.

Background
----------
init.rc:594 starts /sbin/adbd with no `user` line, so init spawns it as root;
adbd drops itself. The drop lives in adb_main (0x8d30, the function holding the
`movw r0,#5555` default-port constant) and is reached as:

    _start 0x8b00 -> __libc_init -> main 0x90d2 -> 0x915a bl -> adb_main 0x8d30

Inside adb_main the drop is a straight-line block with NO guard -- the KitKat
`if (should_drop_privileges())` test is compiled out, which is why presetting
service.adb.root=1 never changed the outcome on the device:

    0x8dc8            start of block (converge point of the two branches above)
    0x8dfc  blx       setgroups32(10, groups)
    0x8e08..0x8e58    prctl PR_CAPBSET_DROP loop -- drops every capability
                      from the bounding set except 6/7 (CAP_SETGID/CAP_SETUID)
    0x8e5e  blx       setgid32(2000)   AID_SHELL
    0x8e6c  blx       setuid32(2000)   AID_SHELL   <-- the only setuid32 in the binary
    0x8e76            first instruction after the block

Patch
-----
One 2-byte branch over the whole block, at the converge point:

    0x8dc8:  48 8f   ldr r0,[pc,#572]   ->   e0 55   b.n 0x8e76

Why this point and not a NOP over the setuid call:
  * Each drop call's return value is checked (`cmp r0,#0; bne.w 0x8fc4` ->
    exit(1)), so simply NOPing `blx setuid32` leaves r0 = 2000 and sends adbd
    down the error path. A NOP patch would have to also force r0 = 0.
  * Skipping only setuid would leave setgid(AID_SHELL) and, worse, the
    PR_CAPBSET_DROP loop in force. With the bounding set cut to
    {CAP_SETUID, CAP_SETGID}, an exec'd shell gets uid 0 with almost no
    capabilities (no CAP_DAC_OVERRIDE, no CAP_SYS_ADMIN) -- a crippled root.
  * Branching from 0x8dc8 keeps uid 0, gid 0 and the full bounding set.

Safety argument (verified, see tools/adbd_sweep.py):
  * Of the 56 branches in adb_main, none lands inside (0x8dc8, 0x8e76) and none
    enters the block from outside; the sole entry is the fallthrough at 0x8dc8.
  * Registers written in the block (r0,r1,r2,r3,r5,r6,r7,ip) are all
    re-assigned after 0x8e76 before any read: r0@0x8e76, r5@0x8e78, r1@0x8e9a,
    r6@0x8ec4, and r7 on every path (0x8eaa / 0x8ebe) before the cbnz at 0x8f12.
  * r4 (is_daemon, set 0x8d38, read 0x8f60) and r8 (set 0x8d3a, read 0x8f92)
    are not touched by the block.
  * The block's only stack writes are the groups array at sp+8..sp+44, read
    solely by the skipped setgroups32. Later code uses sp+52/+56/+148 only.

Usage:
  python3 -I tools/patch_adbd.py <in adbd> <out adbd>
  python3 -I tools/patch_adbd.py <in adbd> --verify     (check an already-patched file)
"""
import hashlib
import shutil
import sys

PRISTINE_SHA256 = "fa61b5e54453729f193c92fb2c8a46416ec4555e805b4f897e7c35c4a64ac173"

# (file offset, vaddr, expected original bytes, patched bytes, description)
PATCHES = [
    (
        0x0DC8,
        0x8DC8,
        bytes.fromhex("8f48"),
        bytes.fromhex("55e0"),
        "ldr r0,[pc,#572] -> b.n 0x8e76  (skip setgroups/CAPBSET_DROP/setgid/setuid)",
    ),
]


def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    src = sys.argv[1]
    verify_only = len(sys.argv) > 2 and sys.argv[2] == "--verify"
    dst = None if verify_only else sys.argv[2]

    before = sha256(src)
    print(f"input : {src}")
    print(f"sha256: {before}")
    if not verify_only and before != PRISTINE_SHA256:
        raise SystemExit(
            f"refusing to patch: expected pristine adbd {PRISTINE_SHA256}"
        )

    if verify_only:
        blob = open(src, "rb").read()
        ok = True
        for off, vaddr, orig, new, desc in PATCHES:
            got = blob[off:off + len(new)]
            state = "PATCHED" if got == new else (
                "PRISTINE" if got == orig else "UNKNOWN")
            if got != new:
                ok = False
            print(f"  0x{off:05x} (vaddr 0x{vaddr:x}): {got.hex()}  {state}")
        print("verify:", "all patches present" if ok else "NOT fully patched")
        return

    shutil.copy2(src, dst)
    blob = bytearray(open(dst, "rb").read())
    for off, vaddr, orig, new, desc in PATCHES:
        got = bytes(blob[off:off + len(orig)])
        if got != orig:
            raise SystemExit(
                f"offset 0x{off:x}: expected {orig.hex()}, found {got.hex()}"
            )
        blob[off:off + len(new)] = new
        print(f"  patched file 0x{off:05x} / vaddr 0x{vaddr:x}: "
              f"{orig.hex()} -> {new.hex()}")
        print(f"      {desc}")
    open(dst, "wb").write(bytes(blob))

    after = sha256(dst)
    print(f"output: {dst}")
    print(f"sha256: {after}")
    assert len(blob) == len(open(src, "rb").read()), "size changed"
    print(f"size unchanged: {len(blob)} bytes")


if __name__ == "__main__":
    main()
