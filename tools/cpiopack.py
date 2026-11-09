import gzip, io, os, stat, sys

# Rewrite an Android newc cpio ramdisk, replacing each entry's payload with the
# file of the same path from a source tree (uid/gid/mode/nlink/mtime/order all
# cloned from the original archive). Zero-payload replacement keeps the
# repack byte-faithful except for edited files.
#
# Usage: python3 -I cpiopack.py <ramdisk.cpio.gz> <src_tree> <out.cpio>

orig, src, out = sys.argv[1], sys.argv[2], sys.argv[3]

raw = gzip.open(orig, "rb").read()


def header(data, i):
    magic = data[i:i + 6]
    assert magic == b"070701", "not newc cpio at %d" % i
    fields = [int(data[i + 6 + 8 * k:i + 14 + 8 * k], 16) for k in range(13)]
    namesize = fields[11]
    name = data[i + 110:i + 110 + namesize - 1].decode()
    filesize = fields[6]
    dstart = i + 110 + namesize
    dstart += (4 - (dstart % 4)) % 4
    return fields, name, data[dstart:dstart + filesize], dstart + filesize + (4 - (dstart + filesize) % 4) % 4


def emit(buf, fields, name, payload):
    fields = list(fields)
    fields[6] = len(payload)
    hdr = ("070701" + "".join(f"{v:08x}" for v in fields)).encode()
    nb = name.encode() + b"\x00"
    buf.write(hdr)
    buf.write(nb)
    buf.write(b"\x00" * ((4 - (len(hdr) + len(nb)) % 4) % 4))
    buf.write(payload)
    buf.write(b"\x00" * ((4 - len(payload) % 4) % 4))


o = io.BytesIO()
i = 0
n = 0
while True:
    fields, name, payload, i = header(raw, i)
    if name == "TRAILER!!!":
        emit(o, fields, name, b"")
        break
    fspath = os.path.join(src, name)
    newpay = (open(fspath, "rb").read()
              if (not os.path.islink(fspath) and os.path.isfile(fspath))
              else payload)
    if newpay != payload:
        print("replaced: %-24s %8d -> %8d" % (name, len(payload), len(newpay)))
    emit(o, fields, name, newpay)
    n += 1

outbuf = o.getvalue()
assert raw.count(b"TRAILER!!!") == 1
open(out, "wb").write(outbuf)
print("entries=%d old=%d new=%d" % (n, len(raw), len(outbuf)))
