import base64, hashlib, os, sys, zipfile

# Builds the no-op sideload test package for TA-1324:
#   phase 1: noop_unsigned.zip (4 content entries) + MANIFEST.MF + CERT.SF on disk
#            (shell then signs CERT.SF -> CERT.RSA with openssl + AOSP testkey)
#   phase 2: noop_signed.zip (adds MANIFEST.MF / CERT.SF / CERT.RSA)
#   verify:  re-verify every digest straight from the final zip
#
# Format mirrors the HMD vendor OTA byte-for-byte where it matters:
# legacy SignApk, SHA-1 digests, CRLF, "Created-By: 1.0 (Android SignApk)",
# per-section digests include the trailing blank line (calibrated 17/17 vs vendor).

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.environ.get("VENDOR_OTA", REPO + "/ota/update.zip")
OUTD = REPO + "/build"
KEYS = REPO + "/keys/testkey-aosp"

TAG = sys.argv[2] if len(sys.argv) > 2 else "noop"
SCRIPT = open(os.path.join(REPO, "tools", TAG + ".edify")).read()
DT = (2008, 2, 29, 10, 33, 46)  # vendor's entry timestamps

CONTENT = [
    "META-INF/com/android/metadata",
    "META-INF/com/android/otacert",
    "META-INF/com/google/android/update-binary",
    "META-INF/com/google/android/updater-script",
]


def sha1b64(b):
    return base64.b64encode(hashlib.sha1(b).digest()).decode()


def entries():
    src = zipfile.ZipFile(VENDOR)
    e = {}
    for n in CONTENT[:-1]:
        e[n] = src.read(n)
    e[CONTENT[-1]] = SCRIPT.encode()
    extra = OUTD + "/" + TAG + "_boot.img"
    if os.path.exists(extra):
        e["boot.img"] = open(extra, "rb").read()
    return e


def zentry(zf, name, blob):
    zi = zipfile.ZipInfo(name, date_time=DT)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    zf.writestr(zi, blob)


def mf_sf():
    ents = entries()
    mf = b"Manifest-Version: 1.0\r\nCreated-By: 1.0 (Android SignApk)\r\n\r\n"
    secs = {}
    for n in sorted(ents):
        secs[n] = f"Name: {n}\r\nSHA1-Digest: {sha1b64(ents[n])}\r\n\r\n".encode()
        mf += secs[n]
    sf = (
        b"Signature-Version: 1.0\r\nSHA1-Digest-Manifest: " + sha1b64(mf).encode()
        + b"\r\nCreated-By: 1.0 (Android SignApk)\r\n\r\n"
    )
    for n in sorted(ents):
        sf += f"Name: {n}\r\nSHA1-Digest-Manifest: {sha1b64(secs[n])}\r\n\r\n".encode()
    return mf, sf


def phase1():
    os.makedirs(OUTD, exist_ok=True)
    with zipfile.ZipFile(OUTD + "/" + TAG + "_unsigned.zip", "w") as zf:
        ents = entries()
        for n in sorted(ents):
            zentry(zf, n, ents[n])
    mf, sf = mf_sf()
    open(OUTD + "/MANIFEST.MF", "wb").write(mf)
    open(OUTD + "/CERT.SF", "wb").write(sf)
    print("phase1 done: entries =", len(CONTENT))
    print("  SHA1(MANIFEST.MF) =", sha1b64(mf))


def phase2():
    p = OUTD + "/" + TAG + "_signed.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zentry(zf, "META-INF/MANIFEST.MF", open(OUTD + "/MANIFEST.MF", "rb").read())
        zentry(zf, "META-INF/CERT.SF", open(OUTD + "/CERT.SF", "rb").read())
        zentry(zf, "META-INF/CERT.RSA", open(OUTD + "/CERT.RSA", "rb").read())
        ents = entries()
        for n in sorted(ents):
            zentry(zf, n, ents[n])
    print("phase2 done:", p)


def verify():
    z = zipfile.ZipFile(OUTD + "/" + TAG + "_signed.zip")
    mf, sf = mf_sf()
    checks = {
        "MANIFEST.MF bytes == rebuilt": z.read("META-INF/MANIFEST.MF") == mf,
        "CERT.SF bytes == rebuilt": z.read("META-INF/CERT.SF") == sf,
        "whole-MF digest matches SF attr": sha1b64(mf).encode()
        in sf.split(b"\r\n")[1],
    }
    for entry in sorted(entries()):
        data = z.read(entry)
        line = f"Name: {entry}\r\nSHA1-Digest: {sha1b64(data)}\r\n".encode()
        checks[f"MF digest {entry}"] = line in mf
    bad = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(("OK  " if v else "FAIL"), k)
    print("zip contents:")
    for i in z.infolist():
        print(f"  {i.filename}  {i.compress_type}  {i.file_size} B")
    if bad:
        sys.exit(1)
    print("ALL DIGEST CHECKS PASS")


def find_eocd(zbytes):
    """Return (eocd_pos, eocd_size) of a plain 22-byte EOCD with empty comment,
    asserting the PK\\x05\\x06 magic occurs nowhere after it (mirrors the
    verifier's own anti-truncation scan)."""
    eocd = zbytes.rfind(b"PK\x05\x06")
    assert zbytes.count(b"PK\x05\x06", eocd) == 1, "EOCD magic repeats after EOCD"
    assert eocd + 22 == len(zbytes), "unexpected bytes after EOCD"
    return eocd


def phase3():
    """Build noop_sd.zip: whole-file signature in the EOCD comment (SignApk v1 scheme).

    Layout (AOSP verifier.cpp 6.0/7.0 — the whole-file scheme the device runs):
      [zip body][EOCD(22)][comment: 'signed by SignApk\\0' + PKCS7 + footer(6)]
    signature = PKCS#1 v1.5 RSA-2048 over SHA-1 of everything except the
    comment and the EOCD's 2-byte comment-length field.
    """
    import subprocess
    src = open(OUTD + "/" + TAG + "_signed.zip", "rb").read()
    eocd = find_eocd(src)
    body, eocd_hdr = src[:eocd], src[eocd:]
    payload = body + eocd_hdr[:20]  # everything except comment + its 2-byte length
    open(OUTD + "/payload.bin", "wb").write(payload)
    subprocess.run(
        ["openssl", "dgst", "-sha1", "-sign",
         KEYS + "/testkey_priv_nocrypt.pem",
         "-out", OUTD + "/raw_signature.bin", OUTD + "/payload.bin"],
        check=True,
    )
    sig = open(OUTD + "/raw_signature.bin", "rb").read()
    assert len(sig) == 256, "unexpected RSA signature size"

    # wrapper = our CERT.RSA (byte-identical to vendor except sig); swap its
    # signature OCTET STRING for the whole-file signature.
    wrapper = open(OUTD + "/CERT.RSA", "rb").read()
    m = b"\x04\x82\x01\x00"  # OCTET STRING header, 256-byte content
    pos = wrapper.rfind(m)
    assert pos >= 0 and pos + 4 + 256 == len(wrapper), "unexpected OCTET STRING layout"
    wrapper = wrapper[:pos + 4] + sig
    assert len(wrapper) == 1714, "wrapper length changed"

    sig_start = len(wrapper) + 6
    comment_len = len(b"signed by SignApk\x00") + len(wrapper) + 6
    assert comment_len < 65536
    comment = (b"signed by SignApk\x00" + wrapper
               + sig_start.to_bytes(2, "little") + b"\xff\xff"
               + comment_len.to_bytes(2, "little"))
    new_eocd = eocd_hdr[:20] + comment_len.to_bytes(2, "little")
    assert b"PK\x05\x06" not in new_eocd[4:]
    assert b"PK\x05\x06" not in comment
    final = body + new_eocd + comment
    open(OUTD + "/" + TAG + "_sd.zip", "wb").write(final)
    print("phase3 done: body=%d + EOCD + comment %d (signature_start=%d)"
          % (len(body), len(comment), sig_start))


def verify2():
    """Re-implement the device's verify_file (whole-file scheme) and RSA-verify."""
    import subprocess
    fin = open(OUTD + "/" + TAG + "_sd.zip", "rb").read()
    footer = fin[-6:]
    assert footer[2] == 0xFF and footer[3] == 0xFF, "footer magic wrong"
    comment_size = footer[4] + (footer[5] << 8)
    signature_start = footer[0] + (footer[1] << 8)
    assert signature_start > 6, "signature start in footer"
    print("comment is %d bytes; signature %d bytes from end" % (comment_size, signature_start))
    eocd_size = comment_size + 22
    signed_len = len(fin) - eocd_size + 22 - 2
    eocd = fin[len(fin) - eocd_size:]
    assert eocd[:4] == b"PK\x05\x06", "EOCD magic"
    hits = [i for i in range(4, eocd_size - 3) if eocd[i:i + 4] == b"PK\x05\x06"]
    assert not hits, "extra EOCD marker in comment region"

    payload = fin[:signed_len]
    open(OUTD + "/payload2.bin", "wb").write(payload)
    import hashlib
    if os.path.exists(OUTD + "/payload.bin"):
        assert hashlib.sha1(payload).digest() == hashlib.sha1(
            open(OUTD + "/payload.bin", "rb").read()).digest(), "payload drift"

    # extract the signature OCTET STRING from the comment's PKCS7, same walk
    # the device does (last OCTET STRING after 4 plain SEQUENCE elements)
    cstart = len(fin) - comment_size
    assert fin[cstart:cstart + 18] == b"signed by SignApk\x00", "comment header"
    wrapper = fin[cstart + 18:cstart + 18 + 1714]
    assert cstart + 18 + 1714 == len(fin) - 6, "wrapper/footer boundary"
    m = b"\x04\x82\x01\x00"
    pos = wrapper.rfind(m)
    assert pos >= 0 and pos + 4 + 256 == len(wrapper), "octet string layout"
    octet_sig = wrapper[pos + 4:pos + 260]
    assert octet_sig == open(OUTD + "/raw_signature.bin", "rb").read(), "sig mismatch in wrapper"

    if not os.path.exists(OUTD + "/pubkey.pem"):
        subprocess.run(["sh", "-c",
            "openssl x509 -pubkey -noout -in %s > %s"
            % (KEYS + "/testkey.x509.pem", OUTD + "/pubkey.pem")],
            check=True)
    r = subprocess.run(
        ["openssl", "dgst", "-sha1", "-verify", OUTD + "/pubkey.pem",
         "-signature", OUTD + "/raw_signature.bin", OUTD + "/payload2.bin"],
        capture_output=True, text=True)
    print("rsa-verify:", r.stdout.strip(), r.stderr.strip())
    assert r.returncode == 0 and "Verified OK" in r.stdout, "RSA verify failed"

    z = zipfile.ZipFile(OUTD + "/" + TAG + "_sd.zip")
    assert len(z.namelist()) == len(entries()) + 3, "zip entries changed"
    assert z.read("META-INF/MANIFEST.MF") == open(OUTD + "/MANIFEST.MF", "rb").read()
    print("ALL DEVICE-SCHEME CHECKS PASS")


if __name__ == "__main__":
    {"phase1": phase1, "phase2": phase2, "verify": verify,
     "phase3": phase3, "verify2": verify2}[sys.argv[1]]()


