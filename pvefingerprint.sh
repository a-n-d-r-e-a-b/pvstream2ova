#!/bin/sh
# pvefingerprint — Create VM fingerprint before OVA export.
# Run inside the VM as root. POSIX sh / busybox compatible.
# Usage: pvefingerprint.sh [output_file]

set -eu

OUT="${1:-/root/.pvefingerprint}"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

echo "pvefingerprint v1"          > "$TMP"
echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$TMP"
echo "hostname=$(hostname)"       >> "$TMP"
echo ""                           >> "$TMP"

echo "[os]" >> "$TMP"
if [ -f /etc/os-release ]; then
    grep -E "^(ID|VERSION_ID|PRETTY_NAME)=" /etc/os-release >> "$TMP"
fi
echo "" >> "$TMP"

echo "[disks]" >> "$TMP"
if command -v lsblk >/dev/null 2>&1; then
    lsblk -bno NAME,SIZE,TYPE 2>/dev/null >> "$TMP" || true
else
    cat /proc/partitions 2>/dev/null >> "$TMP" || true
fi
echo "" >> "$TMP"

echo "[mounts]" >> "$TMP"
df -B1 2>/dev/null | tail -n +2 | \
    grep -v "^tmpfs\|^udev\|^devtmpfs\|^overlay\|^shm\|^efivarfs\|^ramfs" | \
    awk '{print $6, $2, $3, $4}' >> "$TMP" || true
echo "" >> "$TMP"

echo "[uuids]" >> "$TMP"
blkid 2>/dev/null | grep -o 'UUID="[^"]*"' >> "$TMP" || true
echo "" >> "$TMP"

echo "[files]" >> "$TMP"
for f in /etc/hostname /etc/fstab /etc/machine-id /etc/os-release /etc/passwd /etc/group; do
    if [ -f "$f" ]; then
        hash=$(sha256sum "$f" | awk '{print $1}')
        size=$(stat -c%s "$f" 2>/dev/null || wc -c < "$f")
        echo "$f sha256=$hash size=$size" >> "$TMP"
    fi
done
KERNEL=$(ls /boot/vmlinuz* 2>/dev/null | tail -1 || true)
if [ -n "$KERNEL" ] && [ -f "$KERNEL" ]; then
    hash=$(sha256sum "$KERNEL" | awk '{print $1}')
    size=$(stat -c%s "$KERNEL" 2>/dev/null || wc -c < "$KERNEL")
    echo "$KERNEL sha256=$hash size=$size" >> "$TMP"
fi
echo "" >> "$TMP"

echo "[counts]" >> "$TMP"
df -B1 2>/dev/null | tail -n +2 | \
    grep -v "^tmpfs\|^udev\|^devtmpfs\|^overlay\|^shm\|^efivarfs\|^ramfs" | \
    awk '{print $6}' | while read -r mp; do
    count=$(find "$mp" -xdev -type f 2>/dev/null | wc -l || echo "?")
    echo "$mp files=$count" >> "$TMP"
done

mv "$TMP" "$OUT"
echo "Fingerprint saved to $OUT"
wc -l < "$OUT" | awk '{print "  " $1 " lines"}'
