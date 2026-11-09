# Bananarchy Linux on the Nokia 6300 4G (nokia-leo)

Dual-boot [Bananarchy Linux](https://bananarchy.top/) (Alpine/postmarketOS, keypad-first)
on a TA-1324 **alongside an intact KaiOS**, with no EDL loader.

Verified working: kernel 6.12.1-msm8916, Xorg + dwm + fcitx5 up, rootfs on the
microSD, zero crashed services, SSH over USB.

```
PRETTY_NAME="Bananarchy Linux a1108 \"dejavu\" (msm8909)"
/dev/mmcblk1p2   29G  904M   27G   4% /
```

## How the dual-boot works

The manual flashes `lk2nd` to **`boot`**, which would leave KaiOS with no kernel.
Instead put it on **`recovery`**:

| action | path |
| --- | --- |
| power on | aboot → `boot` (p29) → **KaiOS, untouched** |
| Power + `*` | aboot → `recovery` (p33) → **lk2nd** → microSD `pmOS_boot` → Bananarchy |

aboot does not verify boot/recovery signatures on this device, so an unsigned
lk2nd loads fine. You lose stock recovery — acceptable, because once KaiOS is
rooted `dd` writes partitions directly and the recovery/edify route is obsolete.
lk2nd checks the microSD for `pmOS_boot` *before* internal eMMC, so the whole OS
lives on the card.

Prerequisites: rooted KaiOS with `adb` as uid 0 (see `README.md`), a microSD, and
an **x86_64 Linux** build host with Docker.

## Three upstream bugs you must work around

These are bugs in Bananarchy's build, not in your setup. All three bite on a
`nokia-leo` build; report them upstream if they are still present.

**1. `version.sh` is date-pinned.** It derives the version from *today's* date
(`<year-letter><MM><DD>`), while `check-rebuild.sh` asserts the literal release
date `a1108`. On any other day preflight fails and `set -eu` aborts the build.

```sh
export BANANARCHY_VERSION=a1108    # also resolves the codename to "dejavu"
```

**2. The build is x86_64-only.** `rebuild-image.sh` hardcodes
`cp /lib/ld-musl-x86_64.so.1` (lines 375 and 387). The script copies *host-arch*
tools into the armv7 rootfs and chroots in, so armv7 never executes — but that
makes it arch-specific. On an arm64 host, change both lines to
`ld-musl-aarch64.so.1`.

**3. `apk --no-scripts` leaves the rootfs without busybox applet symlinks.**
This is the one that silently breaks boot. Alpine's busybox package does not ship
`/sbin/init` as a file — it is created by the post-install script running
`busybox --install -s`. With `--no-scripts`, **none** of the ~300 applet symlinks
are created. The only survivor is `/bin/sh`, which `rebuild-image.sh` happens to
recreate by hand. `switch_root` then execs a nonexistent `/sbin/init` and dies:
kernel alive, zero userspace, console frozen on the initramfs's last message.
Fix is below.

The same flag also skips **package user/group creation**, which bites later and
less obviously. `avahi-daemon`, for example, fails with `Failed to find user
'avahi'`. Create the account by hand, then reload D-Bus so its policy can
resolve the now-existing user (do **not** restart D-Bus — that kills the Sxmo
session):

```sh
addgroup -S -g 86 avahi
adduser -S -D -H -h /dev/null -s /sbin/nologin -G avahi -g avahi -u 86 avahi
dbus-send --system --print-reply --dest=org.freedesktop.DBus / \
          org.freedesktop.DBus.ReloadConfig
rc-service avahi-daemon start && rc-update add avahi-daemon default
```

Expect other services to need the same treatment; symptoms are always a
missing user or group rather than anything that looks like a packaging bug.

## Build

Must run on x86_64 Linux with a **native filesystem**. Building on macOS fails:
the rootfs lands on a Docker bind mount where APFS is case-insensitive and
virtiofs lacks the hardlink/atomic-rename semantics apk needs — expect ~3400
`failed to commit` / `failed to extract` errors. The same build on ext4 gives
zero.

```sh
git clone https://code.luxferre.top/luxferre/bananarchy.git && cd bananarchy
BANANARCHY_VERSION=a1108 ./rebuild-image.sh nokia-leo --mkinitfs
# -> images/bananarchy-nokia-leo.img  (MBR: 256 MiB ext2 pmOS_boot + ext4 pmOS_root)
```

`nokia-leo` is a first-class target, but its files live inside
`devices/nokia-sparkler/` (leo and the 8000 4G are siblings):
`deviceinfo.nokia-leo`, `qcom-msm8909-nokia-leo.dts`, and the leo-specific
`nokia,leo-gc9305-hlt-24-panel.bin` (2.4" GC9305, vs sparkler's 2.8" GC9307).

Note `rebuild-image.sh` **`exec`s** into `docker run`, so `pgrep -f
rebuild-image.sh` stops matching while the build is still running. Wait on the
container, not the script name.

## Fix 1 (pre-boot, on the card) — SD card detection (DTB)

**Symptom:** the initramfs prints `ERROR: failed to mount subpartitions!` and
drops to its debug shell. `ls /dev/mmcblk1*` finds nothing; `dmesg` shows `mmc1:
SDHCI controller on 7864900.sdhci` registering but no card.

**Cause:** the leo port inherits sparkler's card-detect pin. The DTS declares
`cd-gpios = <&msmgpio 38 GPIO_ACTIVE_LOW>`, but on the 6300 4G that pin reads
**high** with a card inserted (`/sys/kernel/debug/gpio` → `gpio38 : in high`), so
the kernel concludes the slot is empty forever. lk2nd polls instead of using CD,
which is why it reads the card fine and the kernel then cannot.

**Fix:** make the MMC core poll.

```sh
sed 's/cd-gpios = <0x27 0x26 0x01>;/broken-cd;/' \
    devices/nokia-sparkler/qcom-msm8909-nokia-leo.dts > leo-fixed.dts
dtc -I dts -O dtb -o qcom-msm8909-nokia-leo.dtb leo-fixed.dts
```

Only the external slot has `cd-gpios` (eMMC is `non-removable`), so a global
substitution is safe. lk2nd boots via extlinux, which loads the DTB as a *file*
from `pmOS_boot` — drop the new `.dtb` in place, no boot-image rebuild needed.

A cleaner long-term fix would be finding leo's real CD pin, or flipping the
polarity to active-high; `broken-cd` trades interrupt-driven detection for a
1 Hz poll.

## Fix 2 (pre-boot, on the card) — restore the busybox applet symlinks

Without this the system cannot boot (bug 3 above). Mount `pmOS_root` on the
build host and recreate every applet symlink that is missing, never overwriting
a real binary:

```sh
busybox --list-full | sort | while read p; do
  [ -e "$ROOT/$p" ] || [ -L "$ROOT/$p" ] && continue
  mkdir -p "$ROOT/$(dirname "$p")"
  ln -s /bin/busybox "$ROOT/$p"
done
```

Alpine builds busybox with the same applet set on x86_64 and armv7, so the
host's `--list-full` is the authoritative list. Do **not** try to validate
applet names against `strings` on the armv7 binary — busybox stores them as one
packed NUL-separated blob, so short names like `sed`, `ps` and `vi` never appear
as standalone lines and you will get false negatives.

Afterwards verify: `/sbin/init -> /bin/busybox`, plus `getty`, `reboot`, `grep`,
`sed`, `ps`, `find`, `kill`. A healthy rootfs has ~147 busybox symlinks; a broken
one has 1.

## Install

Write the image to the card on the build host (a card reader, not the phone —
see the adb notes below):

```sh
dd if=images/bananarchy-nokia-leo.img of=/dev/sdX bs=4M conv=fsync
```

Grow the rootfs **now**, on the host — it takes seconds here versus minutes on
the phone, and a nearly-full rootfs produces confusing secondary failures later:

```sh
e2fsck -fy /dev/sdXp2
printf ',+\n' | sfdisk -N 2 --force /dev/sdX   # verify the START sector is unchanged
resize2fs /dev/sdXp2
e2fsck -fy /dev/sdXp2
```

The partition **start must not move** — check it before and after; a moved start
destroys the rootfs. Note the image is built with `-i 8192`, so growing to ~29 GB
allocates ~3.7M inodes (≈900 MB of inode tables). That is why `used` jumps
without installing anything.

Then flash lk2nd to `recovery` from rooted KaiOS:

```sh
adb push lk2nd-msm8909.img /data/local/tmp/lk2nd.img   # binary-safe
adb shell 'md5sum /data/local/tmp/lk2nd.img'           # verify before writing
adb shell 'dd if=/data/local/tmp/lk2nd.img of=/dev/block/bootdevice/by-name/recovery bs=4096'
```

Back up `recovery` first (`adb exec-out 'dd if=... 2>/dev/null' > backup.img`).
Restoring is the same `dd` in reverse, and KaiOS still boots from the untouched
`boot` partition, so you always have a root shell to recover from.

Boot: power off fully, then hold **Power + `*`**.

## adb gotchas on this device

This adbd predates shell protocol v2 (`adb features` is empty), which breaks the
usual tricks:

- `adb shell -T` **does not exist** — it returns nothing.
- Plain `adb shell` allocates a PTY and **corrupts binary streams** (CRLF
  translation). It will silently give you wrong hashes.
- **Read binary with `adb exec-out`, write binary with `adb push`.** Both are
  byte-exact (verify with `md5sum`; there is no `sha256sum` on the device).
- Put `2>/dev/null` **inside** the device-side command. Without stream demuxing,
  `dd`'s "records in/out" chatter is interleaved into stdout and corrupts the
  dump by exactly those bytes.
- `adb shell` consumes the caller's stdin, so it eats the rest of a host-side
  loop or heredoc. Add `</dev/null`. (`ssh` does the same — use `ssh -n`.)
- The device's toolbox `dd` rejects `bs=1M` and has `conv` disabled entirely.
  Use byte counts, and pad images to a 4096 boundary on the host.
- USB drops out under sustained load; a 1.6 GB transfer will not survive. Keep
  on-device transfers small.

## Access over USB

The initramfs and the booted system both bring up a USB gadget. `lsusb` reports
`18d1:d001 Google Inc. Nexus 4 (fastboot)` — that is **not** fastboot, it is
postmarketOS reusing Google's IDs. Check the interface descriptors to tell what
you actually have.

Which port is open identifies the boot stage precisely:

| port | meaning |
| --- | --- |
| 23 open | stuck in the initramfs debug shell (telnet, no password) |
| 22 open | full boot, sshd up |
| neither, pingable | kernel alive, userspace dead — usually a failed `switch_root` |

Ping alone proves nothing: the kernel answers ICMP for a configured interface
regardless of whether userspace is alive.

```sh
ssh user@172.16.42.1        # build default password: 147147
                            # same value unlocks the on-screen passcode;
                            # change it via Sxmo Config -> Passcode
```

For key auth, `chmod g-w /home/user` first — the build leaves it group-writable
and OpenSSH's `StrictModes` silently refuses `authorized_keys`.

Once WiFi works (Fix 3), prefer the LAN. The DHCP address moves and
`wcn36xx` here hands out a **locally-administered, non-persistent MAC** (the
NV blob does not supply one), so router reservations are unreliable. Start
`avahi-daemon` instead and reach the phone by name — macOS and most Linux
desktops resolve `.local` natively:

```sh
ssh user@<hostname>.local
```

Note the hostname is `nokia-argon` on every device regardless of target, so
rename it if you run more than one. Remember to restart avahi afterwards or
mDNS keeps advertising the old name:

```sh
echo nokia-leo > /etc/hostname
sed -i 's/nokia-argon/nokia-leo/g' /etc/hosts
hostname nokia-leo
rc-service avahi-daemon restart
```

The initramfs debug shell is a full root shell and exposes a 32 MB logs disk over
USB mass storage containing `dmesg.txt`, `blkid.txt` and the live `fdt.dtb` —
invaluable for confirming what device tree actually booted.

## Fix 3 (after first boot) — WiFi and modem firmware

**Symptom:** WiFi scans return 0 networks, there is no `wlan0` at all, and
`mmcli -L` reports no modems. `wcn36xx` is not loaded — only `qcom_wcnss_pil`.

```
$ cat /sys/class/remoteproc/remoteproc*/state
offline
offline
```

**Cause:** Bananarchy's overlay ships only the Adreno and panel blobs
(`/lib/firmware/qcom/` contains just `a300_pfp.fw` and `a300_pm4.fw`). The
proprietary Qualcomm firmware is not packaged and nothing mounts the device's
firmware partition, so neither remote processor has anything to load. One
missing-firmware problem kills WiFi *and* the modem.

Note the numbering is the opposite of what the addresses suggest:

| node | address | subsystem |
| --- | --- | --- |
| `remoteproc0` | `4080000` | MSS (modem) — wants `mba.mbn` |
| `remoteproc1` | `a204000` | WCNSS (WiFi/BT) — wants `wcnss.mdt` |

**Fix.** The blobs are on the phone's own partitions. As root on the booted
system (`SUDO_ASKPASS` works non-interactively; `sudo -S` does not):

```sh
printf '#!/bin/sh\necho <passcode>\n' > /tmp/ap.sh && chmod +x /tmp/ap.sh
export SUDO_ASKPASS=/tmp/ap.sh    # then: sudo -A <cmd>

# 1. vendor firmware from the `modem` partition (KaiOS /firmware)
mount -o ro /dev/mmcblk0p25 /mnt/fw          # PARTLABEL=modem
mkdir -p /lib/firmware/qcom
cp -a /mnt/fw/image/wcnss.* /mnt/fw/image/mba.mbn /mnt/fw/image/modem.* /lib/firmware/qcom/

# 2. the kernel requests `mba.mbn`, NOT `qcom/mba.mbn`
cd /lib/firmware && for f in qcom/wcnss.* qcom/mba.mbn qcom/modem.*; do
  ln -sf "$f" ./$(basename "$f")
done

# 3. WiFi calibration from `persist` -- the per-unit factory blob,
#    preferred over the generic copy in /system/etc/firmware/
mount -o ro /dev/mmcblk0p31 /mnt/px          # PARTLABEL=persist
mkdir -p /lib/firmware/wlan/prima
cp /mnt/px/WCNSS_qcom_wlan_nv.bin /mnt/px/WCNSS_wlan_dictionary.dat /lib/firmware/wlan/prima/

# 4. start them (or just reboot -- the kernel auto-starts both at ~t+16s
#    once the files exist)
echo start > /sys/class/remoteproc/remoteproc1/state   # WCNSS
echo start > /sys/class/remoteproc/remoteproc0/state   # modem
```

Success looks like this:

```
remoteproc1: Booting fw image wcnss.mdt -> is now up
wcn36xx: firmware WLAN version 'WCN v2.0 RadioPhy ... 19.2MHz XO' CRM '00261'
wcn36xx: firmware API 1.5.1.2, 41 stations, 2 bssids
remoteproc0: Booting fw image mba.mbn -> MBA booted, loading mpss -> is now up
```

Partition numbers here are for a TA-1324; resolve them by label rather than
copying the numbers:

```sh
for p in /dev/mmcblk0p*; do printf '%s %s\n' "$p" "$(blkid -o value -s PARTLABEL $p)"; done
```

The firmware lives on the SD rootfs, so this persists across reboots. `rmtfs`
must be running (it is, by default) — it serves the modem's NV store from
`modemst1`/`modemst2`/`fsg`/`fsc`.

Then connect normally, on the phone via Sxmo's `xnetworks` or from a shell:

```sh
nmcli device wifi connect "<SSID>" password "<password>"
```

**`wcn36xx` on this chip is 2.4 GHz only** — channels 1/6/11. A 5 GHz-only SSID
will never appear in a scan; that is the hardware, not a bug.

## Input: what `fcitx5-t9` is, and adding predictive T9

`fcitx5-t9` is **Bananarchy's own fcitx5 addon**, not upstream — it does not
exist in the Alpine repos or the fcitx5 project. Source lives at
`src/fcitx5-t9/src/t9.cpp` (~1180 lines, single file) and builds to
`/usr/lib/fcitx5/t9.so`.

**It is multi-tap, not predictive.** "T9" here names the 12-key *layout*, not
Text-on-9-keys prediction. A grep for `dict|wordlist|trie|predict|frequency|
ngram|suggest` over the source returns **zero** matches. It provides six modes
(`abc/Abc/ABC/Sym/Ctl/123`, cycled with `#`), multi-tap timers, long-press, and
a fixed candidate bar at `kFixedPopupRect(1, 289, 2, 290)`.

What it already solves, and what prediction can reuse: keypad scancode mapping,
mode handling, and crucially the candidate UI — `activeItems` + `currentIndex` +
`showPreview()` + `stepCandidate()`. Prediction fills the same list with words
instead of letters. One caveat: multi-tap calls `scheduleCommit()` to
auto-commit after a timeout; predictive mode must **not**, or words commit
themselves mid-typing.

### Build constraint

`scripts/build-fcitx5-t9.sh` cross-compiles **only `t9.cpp`** with
`armv7-alpine-linux-musleabihf-g++` against an ephemeral armv7 sysroot
(`fcitx5-dev` via `apk --root --arch armv7`), inside an x86_64 container — no
qemu or binfmt needed. Adding a second `.cpp` means editing the build script, so
keep new code in `t9.cpp` / `t9.h`. A baseline rebuild reproduces the shipped
`libt9.so` **bit-for-bit** (md5 `b5100e0d9c74fceb2fd8b39be9985f82`), which makes
it easy to tell a real change from a toolchain artefact.

### Dictionary format (built and validated)

Sources: OpenSubtitles frequency lists (`es_50k`, `en_50k`, word + count) plus
command names harvested from the device's own `PATH` — so completions match
what is actually installed rather than a generic list.

| metric | value |
| --- | --- |
| typeable words | 85,251 (es + en + 782 commands) |
| distinct digit sequences | 71,289 |
| **unambiguous sequences** | **62,622 (87.8%)** |
| worst collision | `2666` -> 16 words (`como/cómo/coño/boom/...`) |
| `t9.dict` size | 2.17 MB (278 KB index + 1.9 MB blob) |
| lookup on-device (armv7) | 0.29 ms binary search, **6.3 ms** full predict |

Those 6.3 ms are measured in *Python* on the phone, so C++ has ample headroom;
the format is not the bottleneck.

Layout — `mmap` + binary search, zero parsing or allocation:

```
header : 'T9DX' | u16 ver | u16 rsvd | u32 n_seq | u32 off_index
         u32 off_blob | u32 blob_len
index  : n_seq x u32   -- offset of each sequence group, sorted lexicographically
blob   : [u8 seqlen][seq][u16 n_words]
         then n_words x ( [u8 wordlen][u8 lang][u32 freq][word] )
```

Two decisions worth keeping:

- **Grouped by digit sequence, not by word.** Typing is incremental
  (`2` -> `26` -> `266`), so lookup is a *prefix* query: binary search to the
  first group >= prefix, then walk forward while the prefix still matches.
  Contiguous on disk; the kernel pages in only what is touched.
- **`lang` is a bitmask** (`es=1, en=2, sh=4`), not an enum, so a word can
  belong to several lists without duplicate entries. `find` comes out as `7`
  (Spanish + English + command). This is what makes the language filter
  switchable at runtime.

Accents fold onto their base digit (`ñ`->6, `á`->2). Restricting to
`a-z` + valid accents drops list noise such as `cômo`; concatenations like
`yourjob` and proper nouns still slip through.

### Learning (global, logarithmic)

Ranking is `log(base_freq) + K * times_chosen`, with learned counts in a flat
`word<TAB>count` file.

An **additive** boost on raw frequency does not work and was discarded: base
frequencies span 1..28,000,000, so any constant large enough to lift a rare word
past `como` (14.4M) flattens every distinction among rare words. Measured with
`BOOST=200000`, a word stalled at position 3 and never reached first no matter
how often it was chosen. `log()` collapses that range to 0..17, which makes each
use worth a comparable amount.

Calibration — uses needed to reach first place:

| case | K=0.5 | **K=0.8** | K=1.0 | K=1.5 |
| --- | --- | --- | --- | --- |
| `coño` (vs `como`, 14.4M) | 9 | **6** | 5 | 3 |
| `dime` | 5 | **3** | 3 | 2 |

`K=0.8` is the default: 3-6 uses depending on how popular the rival is, and a
stray pick never buries a common word — `como` drops to second, not out. Keep
`K` in a config file so it is tunable without recompiling. The store costs
~7.2 KB per 500 learned words.

### Keypad conventions

Classic Nokia T9 used `#` to cycle mode, `*` for a symbol grid, `1` for
punctuation, `0` for space. Bananarchy currently differs: `*` is a long-press
`Ctrl` latch and symbols live in a whole `Sym` mode. A Cat S22-style layout
(`#` mode, `*` shift, `1` symbols) therefore means **changing existing key
conventions**, not just adding a mode — worth separating from the prediction
work.

### Keypad hardware: it is not bounce

Measured with `evtest` on `/dev/input/event3`: 20 deliberate presses produced
exactly **20 events**, gaps 316-730 ms, holds 93-150 ms, and **zero** intervals
under 80 ms. There is no switch chatter at normal typing speed, even though the
DTS sets no `debounce-delay-ms` (the `matrix_keypad` driver defaults to 0).

What *does* show up is keys emitting odd codes — `KEY_KBD_LCD_MENU2` (697) with
its own `MSC_SCAN` cell (`0x0f`), i.e. a real physical key mapped to a useless
keycode rather than an electrical phantom. `MSC_SCAN` is the thing to read: it
reports the physical matrix cell and distinguishes a mis-mapped key from genuine
row/column crosstalk. Suspected "ghost" keypresses are worth measuring before
assuming debounce.

### Status bar

`fcitx5-t9` writes its badge to `$XDG_RUNTIME_DIR/sxmo_t9_mode` *and*
`sxmo_status/default/005-input-method`, then concatenates every file in that
directory (sorted by name) and pushes the result to the X root window via
`XStoreName`, dlopening `libX11.so.6` at runtime. That root-window name is what
dwm paints. Known open bug: the badge does not refresh in `st`, although the
mode itself changes correctly.

## Known issues / next steps

- **No cellular voice.** Bananarchy does circuit-switched voice only
  (`ModemManager` + `q6voiced`); there is no VoLTE/IMS. On networks that have
  retired 2G/3G this means no carrier calls. See the VoLTE notes below.
- **WiFi and the modem need vendor firmware installed by hand** (see Fix 3);
  out of the box both remote processors stay offline.
- **Hostname is `nokia-argon`** regardless of target device; cosmetic.
- **The `[abc]` input-mode badge does not refresh in `st`** even though the
  mode changes; it does update in other apps.
- `broken-cd` polls the slot at 1 Hz, costing a little idle power.

### VoLTE, if you want to attempt it

The IMS/SIP stack lives in the **modem baseband**, not the OS — voice is handled
in the baseband much like a 2G CS call. KaiOS only runs Qualcomm's host-side QMI
bridge daemons (`imsqmidaemon`, `imsdatadaemon`, `ims_rtp_daemon`). The Linux
equivalents are `81voltd` (QMI IMS DCM, packaged in Debian), `qcom-imsd` and
`qcom-bpm`; `ModemManager` and `q6voiced` are already present. So it is three
daemons to port, not an IMS stack to write.

The modem firmware already ships carrier provisioning for many operators under
`/firmware/image/modem_pr/mcfg/configs/mcfg_sw/generic/fih/` (215 configs, with
`<APN>ims</APN>` and P-CSCF FQDNs), and the baseband is untouched by an OS swap.

Open question: `ims_rtp_daemon` suggests RTP may run on the application
processor on MSM8909. If so, the three daemons are not sufficient. Settle that
before investing time.
