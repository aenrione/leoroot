# leoroot — Nokia 6300 4G TA-1324 (US) root

Root for the KaiOS 2.5.4 "leo" variant whose US hardware is fused to a private
signing chain with **no public EDL firehose loader**, which makes the standard
BananaHackers EDL route impossible on it.

The route used instead:

> the stock recovery's whole-file verifier trusts the **AOSP 2008 testkey**
> (its modulus is the only key in `/res/keys`), so a testkey-signed OTA zip on
> an SD card runs **arbitrary edify as root** via "Apply update from SD card".
> The recovery is the firehose.

Result: `uid=0 gid=0` adb shell, full capability set, SELinux permissive,
persistent across reboots (it all lives in the boot partition).

## Safety

- Targeted at TA-1324 US (`hw.name=leous`, `securityfused=true`, build 12.00.17.01).
  Check yours before flashing anything.
- **Never accept a system OTA after rooting** — it reverts or bricks the patched boot.
- The flash package touches exactly one partition (`boot`): it dd-backs up the
  current boot to the card, aborts if its own image fails an embedded sha1 guard,
  writes, then prints an md5 readback next to the expected value.
- Keep the pristine `boot.img` dump. Rollback is the same SD-card route, and it
  does not depend on adbd working.

## Layout

| path | what |
| --- | --- |
| `tools/bootpack.py` | unpack / repack AOSP boot images (kernel pass-through, cloned header, exact 32 MiB pad) |
| `tools/cpiopack.py` | rewrite a newc cpio ramdisk, replacing payloads from a tree (headers/mode/order cloned) |
| `tools/patch_adbd.py` | apply / verify the 2-byte adbd privilege-drop patch (sha256-guarded) |
| `tools/adbd_sweep.py` | BL/BLX + ARM-syscall sweeps over a stripped ARM Thumb-2 adbd, to re-locate the drop |
| `tools/build_noop.py` | legacy-SignApk package builder + two independent verifiers |
| `tools/leorecover.edify` | dump `boot` + `recovery` + recovery log to the card |
| `tools/leoroot_v3.edify` | flash package: backup → sha1 guard → write boot → md5 readback |
| `keys/testkey-aosp/` | AOSP test key (CN=Android, SHA-1 `61:ED:37:7E:…:AF:81`) |

`build/` and `ota/` are gitignored. `REPRO-HASHES.md` pins every artifact hash.

### About the key

`keys/testkey-aosp/` is Android's **publicly published test key**, shipped in
AOSP at `build/target/product/security/`. It is not a secret and grants nothing
by itself — the finding here is that this device's *production* recovery trusts
it. The private half is the DER `testkey.pk8`; decode it once before signing:

```sh
openssl pkcs8 -inform DER -nocrypt \
    -in keys/testkey-aosp/testkey.pk8 \
    -out keys/testkey-aosp/testkey_priv_nocrypt.pem   # gitignored
```

## Build and flash

```sh
export VENDOR_OTA=/path/to/vendor/update.zip   # byte-template for metadata/otacert/update-binary
python3 -I tools/build_noop.py phase1 leoroot_v3
openssl smime -sign -binary -noattr -md sha1 -outform DER \
    -signer keys/testkey-aosp/testkey.x509.pem \
    -inkey  keys/testkey-aosp/testkey_priv_nocrypt.pem \
    -in build/CERT.SF -out build/CERT.RSA          # must come out 1714 B
python3 -I tools/build_noop.py phase2  leoroot_v3
python3 -I tools/build_noop.py phase3  leoroot_v3
python3 -I tools/build_noop.py verify  leoroot_v3
python3 -I tools/build_noop.py verify2 leoroot_v3
```

For a boot-carrying tag, put the image at `build/<tag>_boot.img` first. Then copy
`build/<tag>_sd.zip` to a FAT32 card → **reboot recovery → Apply update from SD
card** → compare the two md5 lines on screen.

Producing the boot image itself, once you have a dump (`leorecover` gets you one):

1. `python3 -I tools/bootpack.py boot.img work/` → `kernel`, `ramdisk.img`;
   extract with `gzip -dc ramdisk.img | (cd tree && cpio -id --no-absolute-filenames)`
2. edit the tree (bmndc recipe): `default.prop` → `ro.secure=0`, `ro.debuggable=1`;
   comment out `selinux.reload_policy 1` in `init.rc`; append `setenforce 0` to
   `init.qcom.early_boot.sh`; patch `sbin/adbd` with `tools/patch_adbd.py`
3. `python3 -I tools/cpiopack.py work/ramdisk.img tree work/new.cpio`
4. `python3 -I tools/bootpack.py --repack boot.img work out.img "androidboot.selinux=permissive enforcing=0"`

Same build on another TA-1324: the zip flashes as-is (the boot image carries no
per-unit data). Different build: redo the above, and if adbd's sha256 differs the
patcher refuses — re-locate the drop first (below).

## The adbd patch

init spawns adbd as root (`init.rc`: `service adbd /sbin/adbd --root_seclabel=…`,
no `user` line), so **the binary drops privileges itself** — in a straight-line
block inside `adb_main` with **no guard**. KitKat's `should_drop_privileges()` is
compiled out in this build, which is why `ro.secure=0` alone gives no root shell
and `service.adb.root=1` changes nothing.

- syscall stubs are **ARM-mode** (Thumb callers decode as `blx`). The stub at
  0x2a1a8 is `getuid32` (NR 199) — easy to mistake for setuid32; the real
  `setuid32` stub (NR 213) is 0x2a4dc.
- drop block, 0x8dc8 → 0x8e76 inside `adb_main` (0x8d30): `setgroups32`,
  a `prctl(PR_CAPBSET_DROP)` loop, `setgid32(2000)`, `setuid32(2000)`.
- patch at file offset / vaddr **0x8dc8**: `8f 48` (`ldr r0,[pc,#572]`) →
  `e0 55` (`b.n 0x8e76`), skipping the whole block → uid 0, gid 0, full
  bounding set.

Branching over the block rather than NOPing the `setuid` call is deliberate: each
call's return is checked (`cmp r0,#0; bne` → `exit(1)`), so a NOP leaves r0=2000
and takes the error path; and skipping only `setuid` leaves `CAPBSET_DROP` alive,
giving uid 0 with no `CAP_DAC_OVERRIDE` — a crippled root.

For another build: `adbd_sweep.py <adbd> armsvc` lists the privilege-changing
stubs, then `adbd_sweep.py <adbd> bl --target <setuid32 stub>` finds the (single)
call site. Read the surrounding code before choosing a branch target.

## Gotchas

- The verifier uses the **AOSP whole-file scheme** (footer `[sig_start LE16][FF
  FF][comment_len]`, RSA-2048 PKCS#1 v1.5 over SHA-1, PKCS7 in the EOCD comment),
  *not* the JAR/MANIFEST scheme.
- `adb sideload` is broken on leo recovery ("unknown volume", `Total xfer 0.00x`)
  — the SD-card route is the one that works.
- edify `sha1_check` hashes the **string value** of its argument, never a file.
  The real guard is `sha1_check(package_extract_file("boot.img"), "<sha1>")` —
  the 1-arg `package_extract_file` returns the content bytes.
- Card `dd`: `bs=4096` works, `bs=1024` silently loses data without flush. Leave
  the card ~30 s before ejecting. `mount("/system")` before any `run_program`
  (toolbox lives there).
- Signing: **without `-md sha1`, openssl signs sha256** and the device rejects it;
  and a wrong `-inkey` path yields a **0-byte CERT.RSA with no loud failure**.

## Not included

Device firmware and vendor blobs (pristine `boot.img` / `recovery.img`, adbd
binaries, the vendor OTA) are proprietary and stay out of this repo;
`REPRO-HASHES.md` pins their hashes so results stay checkable.
