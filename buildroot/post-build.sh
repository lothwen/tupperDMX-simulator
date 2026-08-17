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
#   /opt/tupperdmx/python/     relocatable CPython + every dependency
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

# The interpreter the app runs under. Deliberately NOT the target's own
# python3: buildroot ships 3.14, and several of the heavy dependencies
# (numba/llvmlite in particular) publish no cp314 wheel yet. uv's managed
# CPython is relocatable, so it can be built here and moved to /opt.
PYTHON_VERSION="${TUPPERDMX_PYTHON_VERSION:-3.13}"
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
# to the absolute on-target `/opt/tupperdmx/python/.../PyQt6/Qt6/lib` through
# all three names at once. Right for the image, fatal for the .venv -- the
# loader falls back to the host's system Qt and every QtMultimedia import dies
# on a missing symbol, so `make` silently broke the checkout it was built from.
# Exported rather than passed per-command so a uv step added later inherits it;
# only the unpacked copies are duplicated, the download cache is still shared.
export UV_LINK_MODE=copy

msg() { echo ">>> tupperdmx: $*"; }


# --- 1. the app source ------------------------------------------------------
# Everything the app imports at runtime, minus the build- and dev-only trees.
# /python is excluded rather than deleted: it lives inside APP_DIR but is
# produced by step 3, and --delete would wipe it on every rebuild.
msg "installing the app source into /opt/tupperdmx"
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude '/python' \
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


# --- 3. the interpreter and its dependencies --------------------------------
# Rebuilt only when missing or on a version bump: this downloads ~1.5 GB of
# wheels the first time, and uv's own cache makes later runs near-instant.
want="Python ${PYTHON_VERSION}."
have=$("$APP_DIR/python/bin/python3" -V 2>/dev/null || true)
case "$have" in
    "$want"*) ;;
    *)
        msg "fetching CPython ${PYTHON_VERSION} (relocatable)"
        rm -rf "$APP_DIR/python"
        stage=$(mktemp -d)
        uv python install --install-dir "$stage" "$PYTHON_VERSION"
        # --install-dir leaves a versioned directory plus an absolute symlink
        # alias next to it; take the real directory and give it a fixed name,
        # so the init script has a stable path across patch releases.
        interp=$(find "$stage" -maxdepth 1 -type d -name 'cpython-*' | head -n1)
        if [ -z "$interp" ]; then
            echo "post-build.sh: uv installed no interpreter under $stage" >&2
            exit 1
        fi
        mkdir -p "$APP_DIR"
        mv "$interp" "$APP_DIR/python"
        rm -rf "$stage"
        ;;
esac

msg "installing the locked dependencies"
req=$(mktemp)
# --no-emit-project: the app itself is the source tree from step 1, not a wheel.
uv export --project "$SRC_DIR" --frozen --no-dev --no-emit-project \
    --format requirements-txt -o "$req"
# --target the interpreter's OWN site-packages: it is a real site directory, so
# .pth files are honoured and nothing has to set PYTHONPATH. `sync` (not
# `install`) so a removed dependency disappears from the image too. Nothing
# installed here may share an inode with the cache -- see UV_LINK_MODE above.
uv pip sync --python "$APP_DIR/python/bin/python3" \
    --target "$APP_DIR/python/lib/python${PYTHON_VERSION}/site-packages" "$req"
rm -f "$req"


# --- 4. runtime state -------------------------------------------------------
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
# The overlay carries the init script; make sure it is executable even if the
# checkout lost the mode bit.
[ -f "$TARGET_DIR/etc/init.d/S99tupperdmx" ] && \
    chmod 0755 "$TARGET_DIR/etc/init.d/S99tupperdmx"

msg "done"
