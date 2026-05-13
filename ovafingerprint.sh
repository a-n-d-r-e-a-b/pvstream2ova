#!/bin/sh
# ovafingerprint — Verify VM integrity after OVA import.
# Run inside the imported VM as root. POSIX sh / busybox compatible.
# Usage: ovafingerprint.sh [fingerprint_file]

set -eu

FP="${1:-/root/.pvefingerprint}"
PASS=0; FAIL=0; WARN=0
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }

if [ ! -f "$FP" ]; then
    echo "ERROR: fingerprint not found: $FP"
    echo "Run pvefingerprint.sh on the source VM before export."
    exit 1
fi

echo ""
echo "=================================================="
echo "  ovafingerprint — VM integrity check"
echo "  Fingerprint: $FP"
echo "=================================================="

orig_host=$(grep "^hostname=" "$FP" | cut -d= -f2)
orig_date=$(grep "^date="     "$FP" | cut -d= -f2)
echo ""
echo "  Source  : $orig_host  ($orig_date)"
echo "  Current : $(hostname)  ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

# ── OS ────────────────────────────────────────────────────────────────────────
echo ""
echo "── OS ──"
if [ -f /etc/os-release ]; then
    orig_id=$(awk -F= '/^\[os\]/{f=1;next} /^\[/{f=0} f && /^ID=/{print $2}' "$FP" | tr -d '"')
    curr_id=$(grep "^ID=" /etc/os-release | cut -d= -f2 | tr -d '"')
    if [ "$orig_id" = "$curr_id" ]; then
        ok "OS: $curr_id"
    else
        warn "OS changed: $orig_id → $curr_id"
    fi
fi

# ── filesystems ───────────────────────────────────────────────────────────────
echo ""
echo "── Filesystems ──"
awk '/^\[mounts\]/{f=1;next} /^\[/{f=0} f && NF' "$FP" > "$TMP"
while read -r mp orig_size rest; do
    if [ ! -d "$mp" ]; then
        warn "$mp: directory missing"
        continue
    fi
    curr_size=$(df -B1 "$mp" 2>/dev/null | tail -1 | awk '{print $2}')
    if [ -z "$curr_size" ]; then
        warn "$mp: not mounted"
        continue
    fi
    diff=$(( (curr_size - orig_size) * 100 / (orig_size + 1) ))
    diff=${diff#-}
    if [ "$diff" -le 5 ]; then
        ok "$mp: ${orig_size} bytes (Δ${diff}%)"
    else
        warn "$mp: size differs ${diff}% ($orig_size → $curr_size)"
    fi
done < "$TMP"

# ── key files ─────────────────────────────────────────────────────────────────
echo ""
echo "── Key files ──"
awk '/^\[files\]/{f=1;next} /^\[/{f=0} f && NF' "$FP" > "$TMP"
while read -r path hash_field size_field; do
    orig_hash=$(echo "$hash_field" | cut -d= -f2)
    orig_size=$(echo "$size_field" | cut -d= -f2)
    if [ ! -f "$path" ]; then
        fail "$path: missing"
        continue
    fi
    curr_hash=$(sha256sum "$path" | awk '{print $1}')
    curr_size=$(stat -c%s "$path" 2>/dev/null || wc -c < "$path")
    if [ "$curr_hash" = "$orig_hash" ] && [ "$curr_size" = "$orig_size" ]; then
        ok "$path"
    else
        fail "$path: hash/size mismatch"
    fi
done < "$TMP"

# ── file counts ───────────────────────────────────────────────────────────────
echo ""
echo "── File counts ──"
awk '/^\[counts\]/{f=1;next} /^\[/{f=0} f && NF' "$FP" > "$TMP"
while read -r mp count_field; do
    orig_count=$(echo "$count_field" | cut -d= -f2)
    if [ ! -d "$mp" ]; then continue; fi
    curr_count=$(find "$mp" -xdev -type f 2>/dev/null | wc -l)
    diff=$((curr_count - orig_count))
    diff_abs=${diff#-}
    pct=$((diff_abs * 100 / (orig_count + 1)))
    if [ "$pct" -le 1 ]; then
        ok "$mp: $curr_count files (Δ$diff)"
    else
        warn "$mp: file count differs ${pct}% ($orig_count → $curr_count)"
    fi
done < "$TMP"

# ── network ───────────────────────────────────────────────────────────────────
echo ""
echo "── Network ──"
gw=$(ip route show default 2>/dev/null | awk '/default/{print $3}' | head -1 || true)
if [ -n "$gw" ]; then
    if ping -c1 -W2 "$gw" >/dev/null 2>&1; then
        ok "gateway $gw reachable"
    else
        warn "gateway $gw unreachable"
    fi
else
    warn "no default route"
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo "  PASS: $PASS   WARN: $WARN   FAIL: $FAIL"
echo "=================================================="
echo ""

[ "$FAIL" -eq 0 ]
