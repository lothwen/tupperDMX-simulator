# Buildroot image (x86_64 EFI rack box)

Builds a bootable x86_64 image that comes up straight into `main.py --headless`
— no GUI, the embedded web remote is the only interface.

Derived from buildroot's `pc_x86_64_efi_defconfig`. It assumes buildroot is
checked out **next to this repository**:

```
Projects/
├── buildroot/                 # the buildroot tree
└── tupperDMX-simulator/       # this repo
```

`BR2_ROOTFS_OVERLAY` and the second `BR2_ROOTFS_POST_BUILD_SCRIPT` both point
here through `$(TOPDIR)/../tupperDMX-simulator/`, so nothing has to be copied
into the buildroot tree.

## Build

```bash
cd ../buildroot
cp ../tupperDMX-simulator/buildroot/x86_config .config   # in-tree: .config at TOPDIR
make olddefconfig
make                                                     # image: output/images/disk.img
```

The first build downloads ~400 MB of Python wheels on the **build host**; later
builds hit uv's cache and take seconds. To carry config changes back:

```bash
make menuconfig
cp .config ../tupperDMX-simulator/buildroot/x86_config
```

### Enabling a package can break the next build

Buildroot never re-runs a package whose `.stamp_built` already exists, **even
when its options changed**. So enabling something that `select`s a new sub-option
of an already-built package leaves that package stale, and the build fails
somewhere else entirely. Adding samba4 did exactly this twice:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `configure: error: external uuid library not found` (e2fsprogs) | e2fsprogs `select`s `BR2_PACKAGE_UTIL_LINUX_LIBUUID`, but util-linux was already built `--disable-libuuid` | `make util-linux-dirclean util-linux-libs-dirclean` |
| host python missing `_ssl` | samba4 `select`s `BR2_PACKAGE_HOST_PYTHON3_SSL`, host-python3 already built without it | `make host-python3-dirclean` |

The rule of thumb: after enabling a package, check what its `Config.in`
`select`s, and `make <pkg>-dirclean` any of those that were already built.
`-dirclean` (not `-rebuild`) is what you want — the configure options changed,
so the tree has to be re-extracted and re-configured.

### `make` used to break the checkout it was built from

`post-build.sh` exports **`UV_LINK_MODE=copy`**, and that line is load-bearing.
uv installs by hardlinking (or reflinking) out of `~/.cache/uv`, so without it a
wheel's files in `TARGET_DIR`, in the cache, and in the repository's own `.venv`
are one inode under three names. Buildroot's `target-finalize` then runs
`support/scripts/fix-rpath target`, whose `patchelf --make-rpath-relative` edits
**in place** — it rewrote PyQt6's `$ORIGIN/Qt6/lib` to the absolute on-target
site-packages path through all three at once. Correct for the image, fatal for
the `.venv`: the loader falls back to the host's system Qt and every
`QtMultimedia` import dies with

```
undefined symbol: _Zls6QDebugN17QVideoFrameFormat11PixelFormatE, version Qt_6
```

which takes `uv run python main.py` and `uv run pytest` with it. If it ever
happens again, the cache is poisoned too, so re-syncing alone will not fix it:

```bash
uv cache clean pyqt6 pyqt6-qt6 pyqt6-sip
uv sync --frozen --reinstall-package pyqt6 \
        --reinstall-package pyqt6-qt6 --reinstall-package pyqt6-sip
```

To check a suspect tree: `patchelf --print-rpath .venv/lib/python3.*/site-packages/PyQt6/QtMultimedia.abi3.so`
must print `$ORIGIN/Qt6/lib`, never an absolute on-target path.

### The wheels want manylinux sonames

The dependencies are manylinux (RHEL) wheels; buildroot is not RHEL, and the
two do not always agree on what a library is called. bzip2 is the one that
bites: PyQt6's bundled `libavformat` links `libbz2.so.1`, buildroot installs
`libbz2.so.1.0.<x>` aliased only as `libbz2.so.1.0`. `post-build.sh` adds the
missing symlink.

The failure is silent and misleading. `libffmpegmediaplugin.so` does not load,
so Qt reports

```
No QtMultimedia backends found. Only QMediaDevices, QAudioDevice, QSoundEffect, QAudioSink, and QAudioSource are available.
Failed to initialize QAudioDecoder "Not available"
```

and **no song plays**, because decoding a file is the only route audio takes to
the mixer — while the metronome and the spoken count-in still work, since numpy
synthesises those and `QAudioSink` needs no backend. On the web remote it shows
up only as `Waveform for song N: nothing decoded after 20s`.

To list every soname the target is missing, on the box (busybox has no `ldd`,
so ask the loader directly):

```bash
LD_TRACE_LOADED_OBJECTS=1 /lib/ld-linux-x86-64.so.2 \
    /usr/lib/python3.14/site-packages/PyQt6/Qt6/plugins/multimedia/libffmpegmediaplugin.so \
    | grep 'not found'
```

## What ends up on the target

| Path | What |
| --- | --- |
| `/opt/tupperdmx/` | the app source (no `tests/`, `docs/`, `.venv`, `.git`) |
| `/usr/lib/python3.14/site-packages/` | every locked dependency (~390 MB), under buildroot's own python3 |
| `/usr/bin/uv`, `/usr/bin/uvx` | static musl build, for on-box maintenance |
| `/var/lib/tupperdmx/` | `HOME` and working directory of the service |
| `/etc/init.d/S49alsa` | restores the mixer (see *The audio path*) |
| `/etc/init.d/S99tupperdmx` | the init script |
| `/mnt/tupperdmx/` | the samba share — a bind mount of `/var/lib/tupperdmx` |
| `/etc/samba/smb.conf` | the share definition |

Everything the app writes lands under `/var/lib/tupperdmx`: `logs/`,
`.config/tupperDMX/userprefs.json` and the show projects in
`Documents/TupperDMX/`. Back up that one directory and you have the box.

## The interpreter

The app runs under **buildroot's own `python3`** (3.14), and `post-build.sh`
installs the locked dependencies into its `/usr/lib/python3.14/site-packages`.
That is a real site directory, so `.pth` files are honoured and nothing needs
`PYTHONPATH`; `/opt/tupperdmx` reaches `sys.path` by being `main.py`'s own
directory.

It did not used to work. Buildroot ships 3.14 and numba/llvmlite published no
cp314 wheel, so the build fetched a **relocatable CPython 3.13** through uv and
bundled it under `/opt/tupperdmx/python/`. Those two came in with librosa, and
left with it — every remaining dependency has a cp314 manylinux wheel.

The wheels are still resolved **on the build host**, which is what lets the box
boot with no network at the venue. uv cannot run a target binary to ask what it
supports, so the resolution is pinned explicitly instead:

```
uv pip sync --python-version 3.14 --python-platform x86_64-manylinux_2_28 --target ...
```

`manylinux_2_28` is the oldest floor onnxruntime publishes; the target's glibc
is 2.43, far newer. The version itself is read out of the rootfs rather than
written down, because installing into a directory nothing imports from fails
*silently* — the box boots and dies on `import PyQt6` with no hint why.

**`python-rtmidi` still has no Linux wheel** and is compiled during the build.
That is sound only because host and target are both x86_64/glibc — the same
assumption the manylinux wheels rest on. If a future dependency has no wheel
for buildroot's python, the answer is to go back to a bundled interpreter (it
is in the git history), *not* to build it on the host.

### Enabling the python modules it needs

Buildroot's `python3` is minimal by default — most stdlib C modules are
separate config options, and five of them are load-bearing here:

| Option | Needed by |
| --- | --- |
| `BR2_PACKAGE_PYTHON3_SSL` | aiohttp — the web remote is the only interface |
| `BR2_PACKAGE_PYTHON3_ZLIB` | aiohttp, mutagen, and `.showpack` (`ZIP_DEFLATED`) |
| `BR2_PACKAGE_PYTHON3_DECIMAL` | mutagen, numpy (pulls in `mpdecimal`) |
| `BR2_PACKAGE_PYTHON3_BZIP2` | numpy |
| `BR2_PACKAGE_PYTHON3_XZ` | numpy (pulls in `xz`) |

They are already set in `x86_config`. The list came from running the headless
app and reading back which of buildroot's optional modules `sys.modules`
actually held — `sqlite3`, `curses`, `readline` and `pyexpat` are genuinely
unused, so they stay off.

> **These are configure-time options on an already-built package**, which is
> exactly the trap described above: `make python3-dirclean` before rebuilding,
> or python3 stays as it was and the app dies on `import ssl`.

## Service control

```bash
/etc/init.d/S99tupperdmx {start|stop|restart|status}
```

`stop` sends SIGTERM and waits up to 20 s — `run_headless()` catches it and runs
the same shutdown path as closing the window, which flushes the pending `.mid`
autosave. Ordering matters: S41dhcpcd (an address to serve the remote on),
S49alsa (an unmuted card) and S50pulseaudio (QtMultimedia's audio backend is
libpulse) all run first.

### When the box comes up "working" but ignores its preferences

That is what an app **dying at startup** looks like from outside, and it used to
leave no trace at all. `start-stop-daemon -b` hands the daemon `/dev/null` on
stdin/stdout/stderr, so redirecting the `start-stop-daemon` command captured
nothing the app ever printed: `boot.log` stayed 0 bytes, `logs/` stopped at
`Configuration loaded`, the script printed `OK`, and `userprefs.json` sat there
with its first-launch defaults forever — never gaining `last_show_path`, and
never reflecting a hand-edit, because nothing ever read it.

`start()` now execs the app through a shell that reopens `boot.log` first, so
everything it writes lands there, and it re-checks the pidfile after two seconds
so an immediate death prints `FAIL (died on startup ...)` instead of `OK`. To
diagnose: `head -40 boot.log` — if it stops mid-startup, the last line names the
stage that failed. A death by signal (a missing CPU feature, for instance) prints
nothing at all, so *silence after a truncated boot.log* is itself the diagnosis.

`boot.log` is truncated on every start and holds only the current run, but it is
the console half of the logging system, so on a box that stays up for weeks it
grows without rotating. Set `"console_output": false` under `logging` in
`userprefs.json` once the box is known good.

## The audio path

Four things have to be true before a song is audible, and all four fail the
same silent way: the app runs, the transport advances, the web remote looks
right, and nothing comes out of the box. Nothing in the app notices either —
the master clock is `time.time()`, so playback does not depend on the audio
sink existing.

**1. Qt can only reach the card through PulseAudio.** The PyQt6 wheel's
`libQt6Multimedia.so.6` carries `libpulse.so.0` as a hard `DT_NEEDED` and does
not contain one single ALSA symbol — Riverbank built that Qt with PulseAudio
only, so there is no ALSA path to fall back to, on this image or any other. To
check a wheel before trusting it:

```bash
strings .venv/lib/python3.*/site-packages/PyQt6/Qt6/lib/libQt6Multimedia.so.6 | grep -ci alsa
```

**2. Something has to start the server.** `BR2_PACKAGE_PULSEAUDIO=y` on its own
installs `libpulse` *and* `/usr/bin/pulseaudio`, so the loader is satisfied and
the box looks healthy; only `BR2_PACKAGE_PULSEAUDIO_DAEMON=y` creates the
`pulse` user and installs `S50pulseaudio`. Without it, S99tupperdmx's
`PULSE_SERVER=unix:/var/run/pulse/native` names a socket nothing ever creates —
and exporting `PULSE_SERVER` at all switches off libpulse's autospawn, so there
is no accidental recovery either.

**3. The app has to be allowed to connect.** In system mode the daemon drops to
the `pulse` user, and `protocol-native.c` authorises a client only when its uid
is the *daemon's* (`pulse`, not root), or its gid is `pulse-access`, or it is a
member of `pulse-access`. **There is no exemption for uid 0.** Buildroot's own
users line — `pulse -1 pulse -1 * /var/run/pulse - audio,pulse-access` — puts
only the daemon in that group, so the app, which runs as root for the DMX and
MIDI devices, is refused:

```
PulseAudioService: Connection failure: "Access denied"
```

`post-fakeroot.sh` adds root to `pulse-access`, and it needs a hook of its own
because that group does not exist yet anywhere earlier: mkusers runs from
`fs/common.mk` **inside** the fakeroot script, after target-finalize. post-build
would edit an `/etc/group` with no such line, and an overlay copy of
`/etc/group` would simply be deleted and re-added by mkusers, which owns every
group it manages. Membership, rather than giving just the daemon the right gid
(`start-stop-daemon -c root:pulse-access` would do it), because *Pointing the
box at a show* tells you to run the app by hand from a root shell — that has to
work too.

**4. The card has to be unmuted.** buildroot's alsa-utils installs
`alsa-restore.service` and `alsa-state.service` and nothing else — both systemd
units, on a busybox-init image. So nothing ran `alsactl restore`, and an HDA
codec comes up exactly as the driver left it: Master muted at zero. `S49alsa`
in the overlay is the missing half. It restores `/var/lib/alsa/asound.state`
when there is one and falls back to `alsactl init` when there is not, which
reads the rules in `/usr/share/alsa/init/` and unmutes every playback switch at
75% — so a box built on hardware nobody has seen before is audible without
anyone finding alsamixer first.

| What you see | Which one |
| --- | --- |
| `No QtMultimedia backends found` / `Failed to initialize QAudioDecoder` | not this — a missing soname, see *The wheels want manylinux sonames* |
| `PulseAudioService: pa_context_connect() failed` in `boot.log`, then `No audio output device available` in the log | **2** — no server. `QMediaDevices` offers zero outputs, `defaultAudioOutput()` is null, and `AudioEngine` starts no sink at all |
| `PulseAudioService: Connection failure: "Access denied"` | **3** — the server is up and was reached; root is not in `pulse-access` |
| `Audio sink started on '<device>' (float32, buffer …)` and still silence | **4** — the mixer. Or a pulse sink muted on top of it |

### Pinning the levels

`S49alsa`'s defaults (step 4 above) are a floor, not a mix. To set the real ones:

```bash
alsamixer                     # M unmutes, arrows set the level
su -c 'alsactl store'         # -> /var/lib/alsa/asound.state, survives reboots
```

`stop` stores too, but only as a courtesy: a venue box is as likely to be
unplugged as shut down cleanly and a yanked cord never reaches `rcK`, so
`alsactl store` by hand is the part to rely on.

That file lives on the box, so a reimage loses it. To make a known-good mixer
part of the image, copy it into the overlay — it is restored ahead of
`alsactl init`, so it wins:

```bash
mkdir -p buildroot/overlay/var/lib/alsa
scp tupperdmx@tupperdmx:/var/lib/alsa/asound.state \
    buildroot/overlay/var/lib/alsa/asound.state
```

It is hardware-specific (the control names come from the codec), which is why
it is not committed for you and why the `alsactl init` fallback exists at all —
on a different box `restore` fails and the generic defaults take over.

## File sharing

`/mnt/tupperdmx` is exported over SMB with **no accounts and no password** —
`map to guest = Bad User` maps every unknown user (i.e. everyone) to the guest
account, and the share is `guest only` with `force user = root`, so whatever a
client drops there is readable and writable by the app, which also runs as root.

```
\\TUPPERDMX\tupperdmx          # Windows (nmbd answers the NetBIOS lookup)
smb://tupperdmx/tupperdmx      # macOS / GNOME / KDE
smbclient //tupperdmx/tupperdmx -N
```

Buildroot's samba4 package ships its own `S91smb`, so start-up is automatic —
but **only when `/etc/samba/smb.conf` exists**, which is exactly what the
overlay provides; the init script exits silently without it.

> **Windows clients:** Windows 10/11 refuse guest logons over SMB2/3 by default.
> Reaching the share from Windows needs `AllowInsecureGuestAuth` enabled
> (`gpedit.msc` → Computer Configuration → Administrative Templates → Network →
> Lanman Workstation → *Enable insecure guest logons*). Linux and macOS clients
> connect as-is.

### The share *is* the app's home directory

`/mnt/tupperdmx` is a **bind mount** of `/var/lib/tupperdmx`, so the share root
is everything the app writes — no copying, no sync, one set of bytes under two
names:

```
<share>/logs/tupperDMX_YYYYMMDD.log      the running log
<share>/.config/tupperDMX/userprefs.json machine preferences
<share>/Documents/TupperDMX/*.show/      the show projects
<share>/boot.log                         this run's console output (see below)
```

```
/etc/fstab:
/var/lib/tupperdmx	/mnt/tupperdmx	none	bind	0	0
```

`post-build.sh` maintains that line (leaving buildroot's own entries alone). It
**rewrites** rather than appends, because the bind source has changed once
already and a second entry would silently shadow the first.

Three consequences worth knowing:

- **`hide dot files = no` is load-bearing.** `userprefs.json` lives under
  `.config/`, and samba hides dot files by default — the preferences would be
  invisible to the very clients the share exists for.
- `userprefs.json` is read **at startup**. Editing it over SMB while the app
  runs changes nothing until a restart, and the app rewrites the file on
  `save_config()`, which will overwrite an edit made underneath it. Stop the
  service (`/etc/init.d/S99tupperdmx stop`) before editing by hand.
- A hand-edit the app **cannot parse** costs it `last_show_path`, and with no
  show picker headless that means it comes up in a brand-new empty
  `Untitled.show`. A UTF-8 BOM (which Windows editors add) is tolerated; a
  genuine syntax error is not, so the rejected file is kept as
  `userprefs.json.bad` and the reason goes to `boot.log`.
- Anything written into `/mnt/tupperdmx` while the bind is *not* active is
  shadowed once it is — a mount hides the underlying directory rather than
  merging with it.

## Pointing the box at a show

The box has no show picker — the web remote plays the open show, it does not
choose one — so a `.show` folder dropped into `Documents/TupperDMX/` over the
share is not enough on its own to *select* it. Two ways to say which one:

```bash
# Adopt whatever is already there: with no usable pointer in userprefs.json,
# the newest .show folder in Documents/TupperDMX/ is opened rather than a new
# empty "Untitled.show" being invented. Copy the folder in, delete the
# last_show_path line (or the whole file), restart.
/etc/init.d/S99tupperdmx restart

# Or pin one explicitly, which also becomes the remembered show:
/usr/bin/python3 /opt/tupperdmx/main.py --headless \
    --show /var/lib/tupperdmx/Documents/TupperDMX/Billares2026.show
```

Add `--show ...` to the init script's `start-stop-daemon` line to make it
permanent. An unloadable path logs a warning and falls back to the normal
resolution, so a typo cannot leave the box with no show.

**Editing `userprefs.json` by hand still works** — it is read at startup and
every key round-trips — but `save_config()` rewrites the file, so an edit made
while the service is running is overwritten when it stops. Stop first, or use
`--show` and never touch the file.

### The locale warning is not the problem

```
Detected locale "C" with character encoding "ANSI_X3.4-1968", which is not UTF-8.
Qt depends on a UTF-8 locale, but has failed to switch to one.
```

Expected on this image: no locales are generated (`/usr/lib/locale` does not
exist), so `LC_CTYPE` stays `C`. Python turns UTF-8 mode on by itself in that
case (PEP 540) and the preferences and manifests are read as UTF-8 regardless,
so this line is noise. Silence it by generating a UTF-8 locale in `x86_config`
if it bothers you; nothing about show loading depends on it.

## Config notes

Beyond python/uv/app, `x86_config` enables the runtime library closure of
QtCore/QtGui/QtMultimedia, which is not obvious from the app's dependency list:

- **C++ toolchain** — every manylinux wheel needs `libstdc++`. `libgomp` was
  here for scipy/scikit-learn, which left with librosa; onnxruntime (piper)
  still wants OpenMP, so it stays.
- **mesa3d (softpipe) + the X11 libs** — `libGL.so.1` and `libEGL.so.1` are
  `DT_NEEDED` of `libQt6Gui`, so the loader needs them *even under the offscreen
  platform plugin*. Nothing renders through them.
- **pulseaudio** — `libQt6Multimedia` links `libpulse.so.0`; there is no ALSA
  path in the Qt6 wheels. alsa-lib is still needed underneath it, and its
  sequencer is what python-rtmidi uses. **`BR2_PACKAGE_PULSEAUDIO_DAEMON=y` is
  load-bearing, not a preference** — see *The audio path*. It is a
  configure-time option on an already-built package, so `make
  pulseaudio-dirclean` before rebuilding (see the trap above).
- **alsa-utils** with `ALSACTL` and `ALSAMIXER` — setting the card's levels and
  saving them. `AMIXER` stays off: `alsamixer` works fine over ssh.
- **libkrb5** — `libQt6Network` links `libgssapi_krb5.so.2`.
- **fontconfig, freetype, libxkbcommon, zstd, bzip2** — the rest of the closure.

## The kernel fragment

`board/pc/linux.config` is 90 lines: USB storage and onboard HDA audio, and
nothing else this app needs. `linux.fragment` is merged on top of it through
`BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES`, leaving buildroot's file untouched:

| Added | Why |
| --- | --- |
| `CONFIG_USB_SERIAL` + `_FTDI_SIO` | `dmx/usb_worker.py` resolves an FTDI serial number (VID `0x0403`) to `/dev/ttyUSB*` at 250000 baud 8N2. Without it the dongle is not a tty, `list_ports.comports()` returns nothing, and every device fails with "No FTDI serial port found" |
| `_CP210X`, `_PL2303`, `_CH341`, `USB_ACM` | the other bridge chips on serial gear that ends up in a rack |
| `CONFIG_SND_SEQUENCER` | python-rtmidi binds to the ALSA **sequencer**; without it there are no MIDI ports at all |
| `CONFIG_SND_USB_AUDIO` | USB MIDI controllers *and* USB audio interfaces. The stock config sets only `CONFIG_SND_USB`, which is just the menu gate |
| `CONFIG_BLK_DEV_NVME` | the stock config drives ATA and USB storage only, so an NVMe disk never appears. Built in, not a module: root is on it and there is no initramfs |
| `CONFIG_HIGH_RES_TIMERS` | the stock config sets no timer options, so timers round up to the `CONFIG_HZ=250` periodic tick — 4 ms. The app's 15 ms audio feed and 20 ms playback tick don't care, but the metronome re-arms a single-shot timer per beat, so every click lands up to 4 ms late against a backing track timed in sample offsets |

After editing the fragment, `make linux-reconfigure` — buildroot does not notice
the file changing on its own.

**Do not run the show off an SD card.** A cold 15 MB backing track read at
**0.2 MB/s** through a USB card reader — 95 seconds — and `QAudioDecoder`
blocks on that read, so the backing joins the timeline late and the web remote
reports `Waveform for song N: nothing decoded after 20s`. It looks exactly like
a slow decoder, and it is not: the same file on NVMe decodes in 0.84 s (448×
realtime).

To measure a medium, drop the page cache first or you are timing RAM — which is
what makes the second read of the same song look instant:

```bash
sync; echo 3 > /proc/sys/vm/drop_caches
dd if=<song>.mp3 of=/dev/null bs=1M
```

Worth reading when it comes out slow: `/sys/kernel/debug/mmc*/ios` for the SD
host's negotiated clock and bus width, and the `speed` file up the device's
`/sys` path for the USB link — a reader that enumerated at full-speed is USB
1.1 and caps near 1 MB/s.

## Logging in

sshd's built-in `PermitRootLogin` default is `prohibit-password` and buildroot
ships `sshd_config` with that line commented out, so **root cannot log in over
SSH with a password** — only with a key. `users.table` therefore creates an
ordinary account for it:

```bash
ssh tupperdmx@tupperdmx      # password: tupperdmx
su -                         # busybox su (root password), for anything privileged
```

| | |
| --- | --- |
| uid/gid | pinned to **1000**, not `-2`/auto — automatic IDs shift when the package set changes, and `/home/tupperdmx` would silently change owner across a rebuild |
| password | `tupperdmx` — its own, independent of root's. Written as `=tupperdmx` in the table: clear text, crypt-encoded by `mkusers` using `BR2_TARGET_GENERIC_PASSWD_METHOD` (sha-256, `$5$`) |
| shell / home | `/bin/sh` (busybox ash), `/home/tupperdmx` |
| groups | none. The account is for getting *in*; the app still runs as **root** under S99tupperdmx because it needs the DMX/MIDI devices and writes `/var/lib/tupperdmx` |

The app's logs are world-readable (0644 under a 0755 directory), so this account
can read them without escalating. Editing `userprefs.json` needs `su`.

## Known gaps
- Both passwords are their own names (`root` / `tupperdmx`) and are set in two
  separate places: `BR2_TARGET_GENERIC_ROOT_PASSWD` in `x86_config`, and the
  `=tupperdmx` field in `users.table`. Together with the unauthenticated SMB
  share and the auth-less web remote, this box trusts its LAN completely — keep
  it on the stage network.
- TTS voice models are not prefetched, so the spoken count-in is unavailable
  until the box downloads one (`Spoken count-in unavailable` in the log).
