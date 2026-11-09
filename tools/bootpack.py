import struct, sys, os, hashlib, zlib

# Unpack an AOSP boot.img (header v0/v1): kernel + compressed ramdisk blobs.
# Usage: python3 -I bootpack.py <boot.img> <outdir>
# Repack from work dir:
#   1) python3 -I cpiopack.py <ramdisk.orig> <edited tree> <workdir>/new.cpio
#   2) python3 -I bootpack.py <orig boot.img> <workdir> <out.img> <cmdline-append>
# Repack: header kept byte-identical except kernel cmdline (+append) and the
# new ramdisk_size; kernel pass-through; output zero-padded to full 32 MiB.

REPACK = len(sys.argv) > 1 and sys.argv[1] == "--repack"

if REPACK:
    srcimg, workdir, outimg, cmdline_append = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    sd = open(srcimg, "rb").read()
    assert sd[:8] == b"ANDROID!"
    ks, ka, rs, ra, ss, sa = struct.unpack_from("<6I", sd, 8)
    _, page_size = struct.unpack_from("<2I", sd, 32)
    kernel = open(workdir + "/kernel", "rb").read()
    cpio = open(workdir + "/new.cpio", "rb").read()
    gz = zlib.compressobj(9, zlib.DEFLATED, 31)
    ramdisk = gz.compress(cpio) + gz.flush()

    def align(x):
        return (x + page_size - 1) // page_size * page_size

    hdr = bytearray(sd[:page_size])
    base_cmd = hdr[64:64 + 512].split(b"\x00")[0].decode()
    new_cmd = (base_cmd + " " + cmdline_append).encode()
    assert len(new_cmd) < 512, "cmdline too long: %d" % len(new_cmd)
    hdr[64:64 + 512] = new_cmd + b"\x00" * (512 - len(new_cmd))
    hdr[16:20] = struct.pack("<I", len(ramdisk))
    o = bytes(hdr)
    o += kernel
    o += b"\x00" * (align(page_size + ks) - page_size - ks)
    o += ramdisk
    o += b"\x00" * (32 * 1024 * 1024 - len(o))
    assert len(o) == 33554432, len(o)
    open(outimg, "wb").write(o)
    print("repacked kernel=%d ramdisk=%d cmdline=%d chars" % (ks, len(ramdisk), len(new_cmd)))
    print("sha1(full padded) =", hashlib.sha1(o).hexdigest())
    print("sha256(full) =", hashlib.sha256(o).hexdigest())
    sys.exit(0)

img, out = sys.argv[1], sys.argv[2]
d = open(img, "rb").read()
assert d[:8] == b"ANDROID!", "not a boot image"

ks, ka, rs, ra, ss, sa = struct.unpack_from("<6I", d, 8)
tags_addr, page_size = struct.unpack_from("<2I", d, 32)
cmdline = d[64:64 + 512].split(b"\x00")[0].decode()
print("kernel_size=%d ramdisk_size=%d second_size=%d page_size=%d"
      % (ks, rs, ss, page_size))
print("kernel_addr=%#x ramdisk_addr=%#x tags_addr=%#x" % (ka, ra, tags_addr))
print("cmdline=%s" % cmdline)

os.makedirs(out, exist_ok=True)


def align(x):
    return (x + page_size - 1) // page_size * page_size


kstart = page_size
open(out + "/kernel", "wb").write(d[kstart:kstart + ks])
rstart = align(kstart + ks)
open(out + "/ramdisk.img", "wb").write(d[rstart:rstart + rs])
print("kernel@%d raw=%d  ramdisk@%d raw=%d" % (kstart, ks, rstart, rs))
print("wrote %s/kernel %s/ramdisk.img" % (out, out))
