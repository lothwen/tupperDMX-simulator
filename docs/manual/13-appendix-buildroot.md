# Appendix B. Building a rack image with Buildroot

[Mode 3](#23-mode-3-headless-in-the-rack-run-from-a-tablet) puts the application
on a small board in the rack with no screen, driven entirely from a tablet. This
appendix builds the operating system for that board: a single bootable image
that comes up straight into `main.py --headless`, with the web remote as the
only interface.

There is no login, no desktop and no package manager on the finished box. It
boots, it plays the show, and it serves the remote.

> **The shipped configuration targets x86_64 with EFI firmware** — a mini-PC or
> NUC-class board, not a Raspberry Pi. See [B.10](#b10-other-boards) before
> pointing this at an ARM board.

## B.1 What you need

| | |
|---|---|
| Build host | Linux, x86_64, with a working build toolchain (`make`, `gcc`, `git`, `bash`) |
| Disk | ~30 GB free for the Buildroot tree and output |
| Time | 1–3 hours for the first build; seconds to minutes afterwards |
| Network | Only on the **build host**, and only for the first build |
| Target | An x86_64 board that boots EFI, and something to write the image to |

The finished box needs no network at the venue. Every Python wheel is resolved
and installed on the build host, which is the whole reason the image is as large
as it is.

## B.2 Lay the two trees out side by side

The configuration reaches into this repository with relative paths, so Buildroot
must be checked out **next to it**:

```
Projects/
├── buildroot/                 # the Buildroot tree
└── tupperDMX-simulator/       # this repository
```

Nothing is copied into the Buildroot tree. `BR2_ROOTFS_OVERLAY` and the second
`BR2_ROOTFS_POST_BUILD_SCRIPT` both point back here through
`$(TOPDIR)/../tupperDMX-simulator/`, so the app, the init script and the Samba
configuration are always read from your working copy.

## B.3 Build

```bash
cd ../buildroot
cp ../tupperDMX-simulator/buildroot/x86_config .config
make olddefconfig
make
```

The result is **`output/images/disk.img`** — a complete disk with its EFI
partition and a 3 GB root filesystem.

The first build downloads roughly 900 MB of Python wheels on the build host.
Later builds hit uv's cache and finish in seconds.

## B.4 Write it to the target

`disk.img` is a whole-disk image, so it goes to the device, not to a partition:

```bash
sudo dd if=output/images/disk.img of=/dev/sdX bs=4M status=progress conv=fsync
```

> Check `/dev/sdX` twice. `dd` will overwrite whatever you point it at, without
> asking, including the disk you are working from.

Then boot the target from it with EFI (not legacy/CSM) enabled.

## B.5 What ends up on the box

| Path | What |
|---|---|
| `/opt/tupperdmx/` | the application source (no `tests/`, `docs/`, `.venv`, `.git`) |
| `/opt/tupperdmx/python/` | a relocatable CPython 3.13 with every locked dependency (~900 MB) |
| `/usr/bin/uv`, `/usr/bin/uvx` | a static build, for maintenance on the box |
| `/var/lib/tupperdmx/` | the service's `HOME` and working directory |
| `/etc/init.d/S99tupperdmx` | the init script |
| `/mnt/tupperdmx/` | the file share — a bind mount of the whole data directory |
| `/etc/samba/smb.conf` | the share definition |

**Everything the application writes lands under `/var/lib/tupperdmx`**: `logs/`,
`.config/tupperDMX/userprefs.json`, and the show projects in
`Documents/TupperDMX/`. Back up that one directory and you have the box — and
since that directory *is* the file share ([B.6](#b6-the-file-share)), backing it
up means dragging one folder off the network.

### Why it carries its own Python

The build fetches CPython 3.13 rather than using the target's own interpreter:
Buildroot ships 3.14, and several dependencies publish no wheel for it yet. The
wheels are resolved on the build host — host and target are both x86_64/glibc, so
they are the same wheels the target would have downloaded — which is what lets
the box boot with no network at the venue.

## B.6 The file share

The box exports `/mnt/tupperdmx` over SMB, with no accounts and no password:

```
\\TUPPERDMX\tupperdmx          # Windows
smb://tupperdmx/tupperdmx      # macOS, GNOME, KDE
smbclient //tupperdmx/tupperdmx -N
```

**That share is the application's entire data directory.** It is a bind mount of
`/var/lib/tupperdmx`, so the share root is everything the box writes — no
copying, no sync, one set of bytes under two names:

```
<share>/Documents/TupperDMX/*.show/       the show projects
<share>/logs/tupperDMX_YYYYMMDD.log       the running log
<share>/.config/tupperDMX/userprefs.json  this machine's preferences
<share>/boot.log                          whatever died before logging came up
```

That covers the three things you would otherwise need a screen and a keyboard
for:

**Loading a show.** Copy a `.show` folder into `Documents/TupperDMX/` and it
lands exactly where the application looks for it — no import step. Restart the
service and the show is there. (Or export a `.showpack` bundle from the desktop
application, [7.1](#71-show-the-project), and drop that in instead.)

**Reading the log** when something did not behave at the venue: it is right
there, live, while the box is running.

**Editing the preferences** — the audio device, the I/O routing, the web-remote
port — by hand, for a box that has no Settings dialog to open.

> **Stop the service before editing `userprefs.json`.** It is read once at
> startup, so an edit changes nothing until a restart — and the application
> rewrites the file whenever it saves, which will overwrite your edit from
> underneath you. `/etc/init.d/S99tupperdmx stop`, edit, start.

Two more things worth knowing:

- **Dot files are deliberately visible.** Samba hides them by default, which
  would put `.config/tupperDMX/userprefs.json` out of reach of exactly the
  clients this share exists for.
- Anything written into `/mnt/tupperdmx` while the bind mount is *not* active is
  shadowed once it is — a mount hides the underlying directory rather than
  merging with it.

> **Windows 10 and 11 refuse guest logons over SMB by default.** Reaching the
> share from Windows needs *Enable insecure guest logons* (`gpedit.msc` →
> Computer Configuration → Administrative Templates → Network → Lanman
> Workstation). Linux and macOS clients connect as-is.

## B.7 Running it

```bash
/etc/init.d/S99tupperdmx {start|stop|restart|status}
```

`stop` sends SIGTERM and waits up to 20 seconds; the application catches it and
runs the same shutdown path as closing the window, which flushes the pending
MIDI autosave. **Stop the service rather than pulling the power** if you have
been editing.

Start-up ordering matters and is already handled: the service runs last, after
DHCP has an address to serve the remote on and after the audio server is up.

Open the remote from a tablet on the same network — the address is the box's IP
on the configured port. → [10](#10-the-web-remote)

## B.8 USB DMX and USB MIDI

Buildroot's own kernel configuration for this board is 90 lines — USB storage
and onboard audio, and nothing else this application needs. A **kernel
fragment** (`buildroot/linux.fragment`) is merged on top of it, leaving
Buildroot's file untouched and upgradeable, and it is what makes the rack box
usable with real gear:

| Added | What it gets you |
|---|---|
| `CONFIG_USB_SERIAL` + `CONFIG_USB_SERIAL_FTDI_SIO` | **USB DMX dongles.** The DMX worker resolves an FTDI serial number to a `/dev/ttyUSB*` port and opens it at 250000 baud, 8N2 — DMX512 on the wire. Without this the dongle never appears as a serial port and every device fails with *"No FTDI serial port found"* |
| `CONFIG_USB_SERIAL_CP210X`, `_PL2303`, `_CH341`, `CONFIG_USB_ACM` | the other bridge chips found on serial gear, and Arduino-class boards |
| `CONFIG_SND_SEQUENCER` | **MIDI at all.** The MIDI layer binds to the ALSA *sequencer*; without it the box has no MIDI ports to offer |
| `CONFIG_SND_USB_AUDIO` | **USB MIDI controllers and USB audio interfaces.** The stock configuration only opens the menu gate, not the driver |

So a rack box built from this configuration will enumerate a USB DMX interface
and a USB MIDI controller, and can use a USB audio interface instead of the
board's onboard output.

> If you edit the fragment, run `make linux-reconfigure` before `make`.
> Buildroot does not notice the file changing on its own, and you will get a
> kernel built from the previous contents with no warning.

## B.9 Changing the configuration

```bash
cd ../buildroot
make menuconfig
make
cp .config ../tupperDMX-simulator/buildroot/x86_config   # carry it back
```

Copy the config back into this repository, or the next person to build gets your
old one.

> **Enabling a package can break the next build, somewhere unrelated.**
> Buildroot never re-runs a package that has already been built, *even when its
> options changed*. So enabling something that pulls in a new sub-option of an
> already-built package leaves that package stale, and the failure surfaces
> elsewhere entirely.
>
> After enabling a package, check what its `Config.in` selects, and
> `make <pkg>-dirclean` anything on that list that was already built. Use
> `-dirclean`, not `-rebuild`: the configure options changed, so the tree has to
> be re-extracted and re-configured.

The configuration also carries the runtime library closure that Qt needs but
that the application's dependency list never mentions — a C++ toolchain and
OpenMP for the wheels, Mesa and the X11 libraries because `libQt6Gui` links
`libGL` *even under the offscreen platform*, PulseAudio because `libQt6Multimedia`
links `libpulse`, Kerberos because `libQt6Network` links it, plus fontconfig,
freetype and the compression libraries. Removing any of them stops the box
booting into the application, with a loader error rather than an obvious one.

## B.10 Other boards

The shipped `x86_config` is derived from Buildroot's `pc_x86_64_efi_defconfig`,
and the build assumes the host and the target share an architecture — that is
what makes it safe to resolve the target's Python wheels on the build machine.

Targeting an ARM board (a Raspberry Pi, say) therefore needs more than a
different defconfig: the wheel set has to be resolved for that architecture, and
the static `uv` fetched for it. The overlay — the init script and the Samba
configuration — carries over unchanged.

## B.11 Known gaps

These are real limitations of the current image, not warnings about misuse.

- **The box trusts its network completely.** The root password is still
  Buildroot's default and sshd is enabled; the SMB share has no accounts and no
  password — and it now exposes the preferences and the logs alongside the
  shows; the web remote has no authentication. Keep it on the stage network, and
  do not put it on the venue's guest Wi-Fi.
- **No text-to-speech voice is preloaded**, so the spoken count-in is
  unavailable until the box downloads one — which it cannot do without a
  network. The log says `Spoken count-in unavailable`. The click and the count-in
  bars still work; only the spoken numbers are missing.
