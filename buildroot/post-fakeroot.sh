#!/usr/bin/env bash
# ABOUTME: Buildroot post-fakeroot hook: puts root into pulseaudio's access
# ABOUTME: group, without which the app -- which runs as root -- gets no audio.
#
# Wired up from buildroot/x86_config as BR2_ROOTFS_POST_FAKEROOT_SCRIPT.
#
# Why this needs its own hook rather than living in post-build.sh or the
# overlay: the `pulse-access` group does not exist yet at either of those
# points. Buildroot creates users and groups from mkusers INSIDE the fakeroot
# script (fs/common.mk), which runs after target-finalize -- so post-build.sh
# would edit an /etc/group that has no pulse-access line, and an overlay copy
# of /etc/group would be thrown away, since mkusers deletes and re-adds any
# group it manages. The post-fakeroot hook is the one step that runs after
# mkusers and before the filesystem image is packed.
#
# Why root has to be in it at all: pulseaudio runs in system mode (S50pulse-
# audio), so it drops to the `pulse` user, and protocol-native.c authorises a
# client only if
#
#     creds->uid == getuid()             the DAEMON's uid, i.e. pulse -- not root
#     || creds->gid == gid_of(pulse-access)
#     || uid_in_group(creds->uid, pulse-access)
#
# There is NO exemption for uid 0. Buildroot's own pulseaudio users line
# (`pulse ... audio,pulse-access`) puts only the daemon in that group, so a
# root client is refused with
#
#     PulseAudioService: Connection failure: "Access denied"
#
# and the box plays in silence exactly as if no server were running at all.
# S99tupperdmx starts the app as root because it needs the DMX/MIDI devices,
# and the README's "run it by hand to pin a show" command is a root shell too,
# so the group membership -- not a per-process trick -- is what fixes both.
set -euo pipefail

TARGET_DIR="${1:-${TARGET_DIR:-}}"
if [ -z "$TARGET_DIR" ] || [ ! -d "$TARGET_DIR" ]; then
    echo "post-fakeroot.sh: no usable TARGET_DIR ('$TARGET_DIR')" >&2
    exit 1
fi

# Both names are compiled into pulseaudio (config.h: PA_ACCESS_GROUP), not read
# from a config file, so they cannot be overridden on the box.
ACCESS_GROUP=pulse-access
CLIENT_USER=root

group_file="$TARGET_DIR/etc/group"
if [ ! -f "$group_file" ]; then
    echo "post-fakeroot.sh: no /etc/group in the target rootfs" >&2
    exit 1
fi

if ! grep -q "^${ACCESS_GROUP}:" "$group_file"; then
    echo "post-fakeroot.sh: no '${ACCESS_GROUP}' group in the target rootfs" >&2
    echo "  BR2_PACKAGE_PULSEAUDIO_DAEMON=y is what creates it, and without a" >&2
    echo "  running pulseaudio this image has no audio output at all: the Qt in" >&2
    echo "  the PyQt6 wheel has no ALSA backend to fall back to." >&2
    exit 1
fi

# Idempotent by hand as well as by construction (fs/common.mk rsyncs a fresh
# TARGET_DIR per image), so this stays safe if it is ever run twice.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
awk -F: -v OFS=: -v grp="$ACCESS_GROUP" -v usr="$CLIENT_USER" '
    $1 == grp {
        n = split($4, members, ",")
        for (i = 1; i <= n; i++)
            if (members[i] == usr) { print; next }
        $4 = ($4 == "" ? usr : $4 "," usr)
    }
    { print }
' "$group_file" > "$tmp"
# Rewritten through the SAME inode rather than moved into place: this hook runs
# after the fakeroot script's `chown -h -R 0:0 $TARGET_DIR`, so a fresh file
# would land in the image owned by whoever ran make. Truncating the original
# keeps its 0:0 ownership and 0644 mode with no chown of our own -- which also
# means this script still works when run by hand outside fakeroot.
cat "$tmp" > "$group_file"

echo ">>> tupperdmx: ${CLIENT_USER} added to '${ACCESS_GROUP}' (pulseaudio system mode)"
