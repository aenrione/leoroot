# Reference hashes — TA-1324 (US), build 12.00.17.01

Device partition dumps and adbd binaries are proprietary and not shipped here;
these hashes pin them for reproducibility.

| artifact | value |
| --- | --- |
| pristine `boot.img` (32 MiB part) | md5 `8b64becf13d854dae58ac16f82320519`; sha1 of first 11,904,204 B: `74fc7cdcdd256225f7c777e5fa6de6ebf008205e` |
| pristine `recovery.img` (32 MiB part) | md5 `e6862b42fde409ae5e606c9440f45b0d` |
| pristine `sbin/adbd` (256,748 B) | sha256 `fa61b5e54453729f193c92fb2c8a46416ec4555e805b4f897e7c35c4a64ac173` |
| patched `sbin/adbd` (drop skipped) | sha256 `bfc711290b8cf7d6a2a432576b2663e39d193c3463446dfd5db137b6697df7d0` |
| v2 boot (bmndc recipe, pre-adbd-patch) | sha1 `77258497827bbb949f7d3377648c4730624ff58a` / md5 `0ede37627434033383d0e68b2df1d48b` |
| v3 boot (v2 + adbd patch) | sha1 `5c4f14c069df45d3ae658b00907a6eae3531731d` / md5 `1b1ed420d4f1e07810619c0525ecbb45` |
| `leoroot_v3_sd.zip` (delivered + flashed) | sha256 `b05f9f7de2b838b8a4b10b452b0f3f870cec121fed7f81dcdc59a0ba5dee9510` — the repo build reproduces this byte-identically |

Device verification facts:

- `/proc/cmdline` carries `securityfused=true`, `hw.name=leous`, `model=TA-1324`
  (plus a per-unit `serialno`, which is why the fused US chain has no public loader)
- recovery `/res/keys` (1387 B): record format `{64, n0inv, {n words}}` (e=3);
  the first record's 64 LE modulus words equal the AOSP testkey's modulus
  (`keys/testkey-aosp/testkey.x509.pem`), confirmed word-for-word
- post-flash state: `uid=0(root) gid=0(root)`, `CapEff/CapPrm/CapBnd =
  0000003fffffffff`, `getenforce` = `Permissive`; booted `/sbin/adbd` carries
  `e0 55` at file offset 0xdc8

The adbd patch, for a different build (see `tools/patch_adbd.py` docstring for
the full static map): the only `setuid32` call is at 0x8e6c inside `adb_main`
(0x8d30); the drop block runs 0x8dc8→0x8e76; patch `0x8dc8: 8f48 → 55e0`.
