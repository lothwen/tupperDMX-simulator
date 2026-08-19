#!/usr/bin/env bash
# ABOUTME: Buildroot post-build hook: installs the app tree, a static uv and a
# ABOUTME: prefetched CPython 3.13 + its dependency set into the target rootfs,
# ABOUTME: so the box boots into headless mode with no network at the venue.
#
# Wired up from buildroot/x86_config as the second BR2_ROOTFS_POST_BUILD_SCRIPT
# (board/pc/post-build.sh stays first — it writes the grub config).
#
# Everything here runs on the BUILD HOST. That is the point: the target is
# x86_64/glibc and so is the host, so the manylinux wheels resolved here are the
# same ones the target would download, and the box never needs a network.
#
# Layout it produces:
#   /opt/tupperdmx/            the app source (main.py, app/, core/, ...)
#   /usr/lib/pythonX.Y/site-packages/   every dependency, for buildroot's python3
#   /usr/bin/uv, /usr/bin/uvx  for on-box maintenance
#   /var/lib/tupperdmx/        HOME and working directory of the service
set -euo pipefail

# Buildroot passes TARGET_DIR both as $1 and in the environment.
TARGET_DIR="${1:-${TARGET_DIR:-}}"
if [ -z "$TARGET_DIR" ] || [ ! -d "$TARGET_DIR" ]; then
    echo "post-build.sh: no usable TARGET_DIR ('$TARGET_DIR')" >&2
    exit 1
fi

HERE=$(cd "$(dirname "$0")" && pwd)
SRC_DIR=$(dirname "$HERE")          # the repository root
APP_DIR="$TARGET_DIR/opt/tupperdmx"

UV_VERSION="${TUPPERDMX_UV_VERSION:-0.12.0}"
# The musl build is static: it does not care what the target's libc is.
UV_ASSET="uv-x86_64-unknown-linux-musl.tar.gz"
UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ASSET}"
# Reuse buildroot's download cache so a rebuild does not refetch uv.
DL_DIR="${BR2_DL_DIR:-${BASE_DIR:-$HERE}/dl}/tupperdmx"

# NEVER let anything under TARGET_DIR share an inode with uv's global cache.
# uv's default is to hardlink (or reflink) out of ~/.cache/uv, which makes a
# wheel's files here, in the cache and in the developer's own .venv ONE file
# under three names. Buildroot's target-finalize then runs
# support/scripts/fix-rpath over TARGET_DIR, and `patchelf
# --make-rpath-relative` edits in place: it rewrote PyQt6's `$ORIGIN/Qt6/lib`
# to the absolute on-target site-packages path through all three names at
# once. Right for the image, fatal for the .venv -- the
# loader falls back to the host's system Qt and every QtMultimedia import dies
# on a missing symbol, so `make` silently broke the checkout it was built from.
# Exported rather than passed per-command so a uv step added later inherits it;
# only the unpacked copies are duplicated, the download cache is still shared.
export UV_LINK_MODE=copy

msg() { echo ">>> tupperdmx: $*"; }


# --- 1. the app source ------------------------------------------------------
# Everything the app imports at runtime, minus the build- and dev-only trees.
# Nothing is excluded from --delete any more: /opt/tupperdmx used to hold a
# bundled interpreter under /python, and letting rsync remove it is how an
# image built before that change loses it.
msg "installing the app source into /opt/tupperdmx"
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude '/.git' \
    --exclude '/.github' \
    --exclude '/.venv' \
    --exclude '/buildroot' \
    --exclude '/docs' \
    --exclude '/tests' \
    --exclude '/logs' \
    --exclude '/scripts' \
    --exclude '/dist' \
    --exclude '/build' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    "$SRC_DIR/" "$APP_DIR/"


# --- 2. uv on the target ----------------------------------------------------
msg "installing uv ${UV_VERSION} into /usr/bin"
mkdir -p "$DL_DIR"
if [ ! -f "$DL_DIR/${UV_VERSION}-${UV_ASSET}" ]; then
    curl -sSfL --retry 3 -o "$DL_DIR/${UV_VERSION}-${UV_ASSET}.tmp" "$UV_URL"
    curl -sSfL --retry 3 -o "$DL_DIR/${UV_VERSION}-${UV_ASSET}.sha256" "${UV_URL}.sha256"
    # The published .sha256 names the asset without our version prefix.
    expected=$(awk '{print $1}' "$DL_DIR/${UV_VERSION}-${UV_ASSET}.sha256")
    actual=$(sha256sum "$DL_DIR/${UV_VERSION}-${UV_ASSET}.tmp" | awk '{print $1}')
    if [ "$expected" != "$actual" ]; then
        echo "post-build.sh: checksum mismatch for $UV_ASSET" >&2
        echo "  expected $expected" >&2
        echo "  got      $actual" >&2
        rm -f "$DL_DIR/${UV_VERSION}-${UV_ASSET}.tmp"
        exit 1
    fi
    mv "$DL_DIR/${UV_VERSION}-${UV_ASSET}.tmp" "$DL_DIR/${UV_VERSION}-${UV_ASSET}"
fi
uv_unpack=$(mktemp -d)
trap 'rm -rf "$uv_unpack"' EXIT
tar -xzf "$DL_DIR/${UV_VERSION}-${UV_ASSET}" -C "$uv_unpack" --strip-components=1
install -D -m 0755 "$uv_unpack/uv" "$TARGET_DIR/usr/bin/uv"
install -D -m 0755 "$uv_unpack/uvx" "$TARGET_DIR/usr/bin/uvx"


# --- 3. the dependencies, into buildroot's own python3 ----------------------
# The app runs under the TARGET's interpreter. That is only possible because
# every dependency publishes a cp314 manylinux wheel -- numba and llvmlite,
# which did not, left the project with librosa. If a future dependency has no
# wheel for buildroot's python, the fix is to go back to a bundled interpreter
# (see git history), not to build it here: uv would build it for the host.
#
# The version is read out of the rootfs rather than pinned, because buildroot
# moves it and installing into a directory nothing imports from fails SILENTLY
# -- the box would boot and die on `import PyQt6` with no clue why.
py_lib=$(find "$TARGET_DIR/usr/lib" -maxdepth 1 -type d -name 'python3.*' \
    | sort -V | tail -n1)
if [ -z "$py_lib" ]; then
    echo "post-build.sh: no python3 in the target rootfs" >&2
    echo "  enable BR2_PACKAGE_PYTHON3 and its SSL/ZLIB/BZIP2/XZ/DECIMAL modules" >&2
    exit 1
fi
py_version=${py_lib##*/python}          # e.g. 3.14
site="$py_lib/site-packages"

msg "installing the locked dependencies for target python ${py_version}"
mkdir -p "$site"
req=$(mktemp)
# --no-emit-project: the app itself is the source tree from step 1, not a wheel.
uv export --project "$SRC_DIR" --frozen --no-dev --no-emit-project \
    --format requirements-txt -o "$req"
# Resolved for the TARGET rather than this host: --python-version and
# --python-platform pick the wheels buildroot's interpreter can load, without
# uv ever running it (it cannot -- it is a target binary). manylinux_2_28 is
# the oldest floor onnxruntime publishes; the target's glibc is far newer.
# python-rtmidi still has no Linux wheel and IS built here, which is sound only
# because host and target are both x86_64/glibc.
#
# `sync` (not `install`) so a removed dependency disappears from the image too
# -- that is what strips librosa/scipy/numba out of a tree built before they
# were dropped. Nothing installed here may share an inode with the cache: see
# UV_LINK_MODE above.
uv pip sync --python-version "$py_version" \
    --python-platform x86_64-manylinux_2_28 \
    --target "$site" "$req"
rm -f "$req"


# --- 4. manylinux soname compatibility --------------------------------------
# The wheels are built on manylinux (RHEL), where bzip2's soname is
# `libbz2.so.1`. Buildroot installs the library as libbz2.so.1.0.<x> and
# aliases it only as `libbz2.so.1.0`, so PyQt6's bundled libavformat -- and
# with it libffmpegmediaplugin.so -- does not load. Qt then says "No
# QtMultimedia backends found", QAudioDecoder reports "Not available", and the
# box plays NOTHING: decoding a file is the only way audio reaches the mixer.
# The metronome and the spoken count-in still work (numpy synthesises those,
# and QAudioSink needs no backend), which is exactly what makes this look like
# a slow SBC or a codec problem instead of a missing symlink.
#
# Resolved out of the rootfs rather than written down, for the same reason the
# python version above is.
bz2_lib="$TARGET_DIR/usr/lib/libbz2.so.1"
bz2_real=$(find "$TARGET_DIR/usr/lib" -maxdepth 1 -type f -name 'libbz2.so.1.*' \
    | sort -V | tail -n1)
if [ -e "$bz2_lib" ]; then
    :   # already the name the wheels ask for
elif [ -n "$bz2_real" ]; then
    msg "linking libbz2.so.1 -> $(basename "$bz2_real") for the manylinux wheels"
    ln -sf "$(basename "$bz2_real")" "$bz2_lib"
else
    echo "post-build.sh: no libbz2 in the target rootfs" >&2
    echo "  enable BR2_PACKAGE_BZIP2, or QtMultimedia loses its ffmpeg plugin" >&2
    exit 1
fi


# --- 5. runtime state -------------------------------------------------------
# HOME and working directory of the service: logs/, .config/userprefs.json and
# the show projects all land here, on the writable rootfs.
install -d -m 0755 "$TARGET_DIR/var/lib/tupperdmx"
# The samba share (see overlay/etc/samba/smb.conf). 0755 root-owned is enough:
# the share sets `force user = root`, so guests write as root anyway.
install -d -m 0755 "$TARGET_DIR/mnt/tupperdmx"

# The share IS the app's home directory, bind-mounted onto the share path, so a
# client sees the logs, userprefs.json and the show projects themselves rather
# than copies. inittab runs `mount -a` at sysinit, before rcS starts samba and
# the app. Appended rather than shipped as an overlay /etc/fstab, so buildroot's
# own entries (proc, tmpfs, devpts...) stay whatever the skeleton says.
fstab="$TARGET_DIR/etc/fstab"
if [ -f "$fstab" ]; then
    # Strip whatever a previous build left behind first: the bind source has
    # already changed once, and appending blindly would stack a second mount
    # on /mnt/tupperdmx that shadows the first.
    grep -vE '^# (Expose the app|tupperdmx:)|[[:space:]]/mnt/tupperdmx[[:space:]]' \
        "$fstab" > "$fstab.new"
    mv "$fstab.new" "$fstab"
fi
{
    echo '# tupperdmx: the samba share is the app home directory'
    printf '/var/lib/tupperdmx\t/mnt/tupperdmx\tnone\tbind\t0\t0\n'
} >> "$fstab"
# Samba's persistent state. /var/lib is the only real directory under /var in
# this skeleton -- /var/log, /var/cache and /var/run are tmpfs symlinks, and
# smbd recreates what it needs there on every boot.
install -d -m 0755 "$TARGET_DIR/var/lib/samba"
install -d -m 0700 "$TARGET_DIR/var/lib/samba/private"
# The overlay carries the init scripts; make sure they are executable even if
# the checkout lost the mode bit.
for init in S49alsa S99tupperdmx; do
    [ -f "$TARGET_DIR/etc/init.d/$init" ] && \
        chmod 0755 "$TARGET_DIR/etc/init.d/$init"
done

msg "done"
