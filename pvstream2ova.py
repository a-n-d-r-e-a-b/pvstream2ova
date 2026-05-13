#!/usr/bin/env python3
"""pvstream2ova — Export Proxmox VM to OVA with streaming hole-punch and parallel VMDK compression.

Native VMDK streamOptimized writer: 64 KiB grains compressed in parallel via ThreadPoolExecutor.
Peak disk usage = one VMDK; freed by fallocate hole-punching while streaming into the OVA tar.
Generates OVF 1.0 descriptor and SHA256 manifest (.mf) for VMware/VirtualBox compatibility.

Usage: pvstream2ova.py --vmid <ID> [--out <dir>] [--tmp <dir>] [--name <name>]
                       [--workers N] [--level 1-9] [--log <file>] [--verify]
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import os
import re
import signal
import struct
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

log = logging.getLogger("pvstream2ova")

CHUNK        = 64 << 20
GRAIN_SIZE   = 65536
GRAIN_SECTS  = 128
GT_COVERAGE  = 512
BATCH_GRAINS = 256
VMDK_MAGIC   = 0x564D444B
VMDK_VER     = 3
_MARKER_EOS  = 0
_MARKER_GT   = 1
_MARKER_GD   = 2
_MARKER_FOOT = 3
_DESC_BYTES  = 20 * 512

_libc  = ctypes.CDLL("libc.so.6", use_errno=True)
_PUNCH = 0x01 | 0x02  # FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE


def _punch_hole(fd: int, offset: int, length: int) -> None:
    _libc.fallocate(ctypes.c_int(fd), ctypes.c_int(_PUNCH),
                    ctypes.c_long(offset), ctypes.c_long(length))


# ── TAR ustar (OVA container) ─────────────────────────────────────────────────

def _tar_header(name: str, size: int) -> bytes:
    h = bytearray(512)

    def put_str(val, off, width):
        b = (val.encode() if isinstance(val, str) else val)[:width]
        h[off:off + len(b)] = b

    def put_oct(val, off, width):
        put_str(f"{val:0{width - 1}o}\x00", off, width)

    put_str(name[:99], 0, 100)
    put_oct(0o644, 100, 8)
    put_oct(0, 108, 8)
    put_oct(0, 116, 8)
    put_oct(size, 124, 12)
    put_oct(int(time.time()), 136, 12)
    h[156] = ord("0")
    h[257:265] = b"ustar\x0000"
    h[148:156] = b"        "
    put_str(f"{sum(h):06o}\x00 ", 148, 8)
    return bytes(h)


class OvaWriter:
    def __init__(self, path: Path) -> None:
        self._f = path.open("wb")

    def add_bytes(self, name: str, data: bytes) -> None:
        self._f.write(_tar_header(name, len(data)))
        self._f.write(data)
        pad = (-len(data)) % 512
        if pad:
            self._f.write(b"\x00" * pad)

    def add_file(self, name: str, path: Path,
                 on_progress: Callable[[int, int], None] | None = None) -> None:
        """Stream file into OVA with hole-punching; deletes source when done."""
        size = path.stat().st_size
        self._f.write(_tar_header(name, size))
        fd = os.open(str(path), os.O_RDWR)
        written = 0
        try:
            while written < size:
                to_read = min(CHUNK, size - written)
                chunk   = os.read(fd, to_read)
                if not chunk:
                    break
                self._f.write(chunk)
                _punch_hole(fd, written, len(chunk))
                written += len(chunk)
                if on_progress:
                    on_progress(written, size)
        finally:
            os.close(fd)
        path.unlink()
        pad = (-size) % 512
        if pad:
            self._f.write(b"\x00" * pad)

    def close(self) -> None:
        self._f.write(b"\x00" * 10240)
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


# ── VMDK streamOptimized ──────────────────────────────────────────────────────

def _vmdk_header(capacity_sectors: int, gd_offset: int, rgd_offset: int = 0) -> bytes:
    flags = (1 << 0) | (1 << 16) | (1 << 17)  # newlines | compressed | markers
    buf = bytearray(512)
    struct.pack_into("<I", buf,  0, VMDK_MAGIC)
    struct.pack_into("<I", buf,  4, VMDK_VER)
    struct.pack_into("<I", buf,  8, flags)
    struct.pack_into("<Q", buf, 12, capacity_sectors)
    struct.pack_into("<Q", buf, 20, GRAIN_SECTS)
    struct.pack_into("<Q", buf, 28, 1)           # descriptorOffset
    struct.pack_into("<Q", buf, 36, 20)          # descriptorSize
    struct.pack_into("<I", buf, 44, GT_COVERAGE)
    struct.pack_into("<Q", buf, 48, rgd_offset)
    struct.pack_into("<Q", buf, 56, gd_offset)
    struct.pack_into("<Q", buf, 64, 128)         # overHead: first grain at sector 128
    buf[72] = 0
    buf[73] = ord('\n'); buf[74] = ord(' '); buf[75] = ord('\r'); buf[76] = ord('\n')
    struct.pack_into("<H", buf, 77, 1)           # compressAlgorithm: deflate
    return bytes(buf)


def _vmdk_descriptor(name: str, capacity_sectors: int) -> bytes:
    text = (
        "# Disk DescriptorFile\n"
        "version=1\n"
        "CID=fffffffe\n"
        "parentCID=ffffffff\n"
        'createType="streamOptimized"\n'
        "\n"
        "# Extent description\n"
        f'RDONLY {capacity_sectors} STREAMOPTIMIZED "{name}.vmdk"\n'
        "\n"
        "#DDB\n"
        "\n"
        'ddb.virtualHWVersion = "19"\n'
        'ddb.geometry.cylinders = "0"\n'
        'ddb.geometry.heads = "255"\n'
        'ddb.geometry.sectors = "63"\n'
        'ddb.adapterType = "lsilogic"\n'
    )
    raw = text.encode("ascii")
    return raw + b"\x00" * (_DESC_BYTES - len(raw))


def _meta_marker(num_data_sectors: int, mtype: int) -> bytes:
    buf = bytearray(512)
    struct.pack_into("<QII", buf, 0, num_data_sectors, 0, mtype)
    return bytes(buf)


def _compress_batch(raw: bytes, level: int) -> list[bytes | None]:
    results = []
    offset  = 0
    while offset < len(raw):
        grain = raw[offset:offset + GRAIN_SIZE]
        if not grain:
            break
        results.append(None if not any(grain) else zlib.compress(grain, level=level))
        offset += GRAIN_SIZE
    return results


class _HashingFile:
    """Wraps a writable file to compute SHA256 of everything written."""
    def __init__(self, f) -> None:
        self._f = f
        self._h = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self._f.write(data)
        self._h.update(data)
        return len(data)

    def hexdigest(self) -> str:
        return self._h.hexdigest()


class VmdkWriter:
    def __init__(self, src: str, dst: Path, workers: int, level: int = 1,
                 on_progress: Callable[[int, int], None] | None = None) -> None:
        self._src      = src
        self._dst      = dst
        self._workers  = workers
        self._level    = level
        self._progress = on_progress

    def convert(self) -> str:
        """Convert LVM block device to VMDK streamOptimized; returns SHA256 of output."""
        src_size         = int(subprocess.check_output(["blockdev", "--getsize64", self._src]).strip())
        capacity_sectors = src_size // 512
        n_grains         = (src_size + GRAIN_SIZE - 1) // GRAIN_SIZE

        src_fd = os.open(self._src, os.O_RDONLY)
        try:
            with self._dst.open("wb") as raw_f, \
                 ThreadPoolExecutor(max_workers=self._workers) as pool:
                hf = _HashingFile(raw_f)
                self._write(hf, pool, src_fd, src_size, capacity_sectors, n_grains)
                return hf.hexdigest()
        finally:
            os.close(src_fd)

    def _write(self, f, pool, src_fd, src_size, capacity_sectors, n_grains) -> None:
        name       = self._dst.stem
        cur_sector = 0

        def write_sectors(data: bytes) -> None:
            nonlocal cur_sector
            f.write(data)
            cur_sector += len(data) // 512

        write_sectors(_vmdk_header(capacity_sectors, gd_offset=0xFFFFFFFFFFFFFFFF))
        write_sectors(_vmdk_descriptor(name, capacity_sectors))

        if cur_sector < 128:
            f.write(b"\x00" * ((128 - cur_sector) * 512))
            cur_sector = 128

        grain_offsets = [0] * n_grains
        gd_entries    = []

        def flush_gt(block_start: int) -> None:
            nonlocal cur_sector
            entries = grain_offsets[block_start:block_start + GT_COVERAGE]
            entries += [0] * (GT_COVERAGE - len(entries))
            write_sectors(_meta_marker(4, _MARKER_GT))
            gd_entries.append(cur_sector)           # points to GT data, after marker
            f.write(struct.pack(f"<{GT_COVERAGE}I", *entries))
            cur_sector += 4

        pending      = []
        next_submit  = 0

        def submit_next() -> None:
            nonlocal next_submit
            if next_submit >= n_grains:
                return
            bs        = next_submit
            read_size = min(BATCH_GRAINS * GRAIN_SIZE, src_size - bs * GRAIN_SIZE)
            pending.append((bs, pool.submit(_compress_batch,
                                            os.pread(src_fd, read_size, bs * GRAIN_SIZE),
                                            self._level)))
            next_submit += BATCH_GRAINS

        for _ in range(self._workers * 2):
            submit_next()

        grains_done = 0
        while pending:
            batch_start, future = pending.pop(0)
            submit_next()
            for ci, cdata in enumerate(future.result()):
                gi = batch_start + ci
                if gi >= n_grains:
                    break
                if cdata is not None:
                    grain_offsets[gi] = cur_sector
                    blob = struct.pack("<QI", gi * GRAIN_SECTS, len(cdata)) + cdata
                    pad  = (-len(blob)) % 512
                    f.write(blob + b"\x00" * pad)
                    cur_sector += (len(blob) + pad) // 512
                grains_done += 1
                if grains_done % GT_COVERAGE == 0:
                    flush_gt(grains_done - GT_COVERAGE)
                if self._progress:
                    self._progress(grains_done * GRAIN_SIZE, src_size)

        if grains_done % GT_COVERAGE:
            flush_gt(grains_done - grains_done % GT_COVERAGE)

        gd_data  = struct.pack(f"<{len(gd_entries)}I", *gd_entries)
        gd_pad   = (-len(gd_data)) % 512
        gd_sects = (len(gd_data) + gd_pad) // 512
        write_sectors(_meta_marker(gd_sects, _MARKER_GD))
        gd_data_sector = cur_sector                 # points to GD data, after marker
        f.write(gd_data + b"\x00" * gd_pad)
        cur_sector += gd_sects

        # qemu reads footer at file_size-1536: [FOOT marker][Footer][EOS]
        write_sectors(_meta_marker(1, _MARKER_FOOT))
        write_sectors(_vmdk_header(capacity_sectors, gd_offset=gd_data_sector, rgd_offset=0))
        write_sectors(_meta_marker(0, _MARKER_EOS))


# ── Proxmox helpers ───────────────────────────────────────────────────────────

def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=check, capture_output=True, text=True)


def _lv(action: str, lv: str) -> None:
    _run("lvchange", f"-a{action}", f"pve/{lv}", check=(action == "y"))


def _vm_status(vmid: int) -> str:
    return _run("qm", "status", str(vmid)).stdout.split()[-1]


def _vm_config(vmid: int) -> dict:
    conf_path = Path(f"/etc/pve/qemu-server/{vmid}.conf")
    if not conf_path.exists():
        raise SystemExit(f"VM {vmid} not found: {conf_path}")

    cfg = {"name": f"vm-{vmid}", "cores": 1, "sockets": 1,
           "memory": 1024, "bios": "seabios", "disks": [],
           "scsihw": "lsi", "networks": []}

    for raw_line in conf_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip(); val = val.strip()

        if   key == "name":    cfg["name"]    = val
        elif key == "cores":   cfg["cores"]   = int(val)
        elif key == "sockets": cfg["sockets"] = int(val)
        elif key == "memory":  cfg["memory"]  = int(val)
        elif key == "bios":    cfg["bios"]    = val
        elif key == "scsihw":  cfg["scsihw"]  = val
        elif re.match(r"^net\d+$", key):
            m = re.search(r"bridge=([^,]+)", val)
            if m:
                cfg["networks"].append(m.group(1))
        elif key == "efidisk0":
            log.warning("efidisk0 skipped — EFI NVRAM variables are not portable across hypervisors")
        elif re.match(r"^(virtio|scsi|sata|ide)\d+$", key):
            if "media=cdrom" not in val:
                m = re.search(r"local-lvm:(vm-\d+-disk-\d+)", val)
                if m:
                    cfg["disks"].append(m.group(1))

    cfg["vcpus"] = cfg["cores"] * cfg["sockets"]
    return cfg


# ── OVF 1.0 ───────────────────────────────────────────────────────────────────

def _ovf(name: str, vcpus: int, memory: int, bios: str, disks: list[dict], networks: list[str]) -> bytes:
    refs = "\n".join(
        f'    <File ovf:id="file{i}" ovf:href="{d["name"]}" ovf:size="{d["size"]}"/>'
        for i, d in enumerate(disks)
    )
    disk_elems = "\n".join(
        f'    <Disk ovf:diskId="disk{i}" ovf:fileRef="file{i}"'
        f' ovf:capacity="{d["vsize"]}" ovf:capacityAllocationUnits="byte"'
        f' ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>'
        for i, d in enumerate(disks)
    )
    disk_items = "\n".join(
        f"      <Item>\n"
        f"        <rasd:AddressOnParent>{i}</rasd:AddressOnParent>\n"
        f"        <rasd:ElementName>disk{i}</rasd:ElementName>\n"
        f"        <rasd:HostResource>ovf:/disk/disk{i}</rasd:HostResource>\n"
        f"        <rasd:InstanceID>{20 + i}</rasd:InstanceID>\n"
        f"        <rasd:Parent>4</rasd:Parent>\n"
        f"        <rasd:ResourceType>17</rasd:ResourceType>\n"
        f"      </Item>"
        for i in range(len(disks))
    )

    net_names = networks if networks else ["VM Network"]
    unique_nets = list(dict.fromkeys(net_names))
    net_elems = "\n".join(
        f'    <Network ovf:name="{b}"><Description>Proxmox bridge: {b}</Description></Network>'
        for b in unique_nets
    )
    nic_items = "\n".join(
        f"      <Item>\n"
        f"        <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>\n"
        f"        <rasd:Connection>{bridge}</rasd:Connection>\n"
        f"        <rasd:ElementName>Network Adapter {i}</rasd:ElementName>\n"
        f"        <rasd:InstanceID>{5 + i}</rasd:InstanceID>\n"
        f"        <rasd:ResourceSubType>VmxNet3</rasd:ResourceSubType>\n"
        f"        <rasd:ResourceType>10</rasd:ResourceType>\n"
        f"      </Item>"
        for i, bridge in enumerate(net_names)
    )

    # VMware-specific firmware hint (ovf:required=false → ignored by other tools)
    efi_item = ('      <vmw:ExtraConfig ovf:required="false" vmw:key="firmware" vmw:value="efi"/>\n'
                if bios == "ovmf" else "")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
  xmlns:vmw="http://www.vmware.com/schema/ovf"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <References>
{refs}
  </References>
  <DiskSection>
    <Info>Virtual disks</Info>
{disk_elems}
  </DiskSection>
  <NetworkSection>
    <Info>Virtual networks</Info>
{net_elems}
  </NetworkSection>
  <VirtualSystem ovf:id="{name}">
    <Info>{name}</Info><Name>{name}</Name>
    <OperatingSystemSection ovf:id="96">
      <Info>Guest OS</Info><Description>Debian GNU/Linux</Description>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware</Info>
      <System>
        <rasd:ElementName>Virtual Hardware Family</rasd:ElementName>
        <rasd:InstanceID>0</rasd:InstanceID>
        <rasd:VirtualSystemType>vmx-19</rasd:VirtualSystemType>
      </System>
      <Item>
        <rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits>
        <rasd:ElementName>{vcpus} vCPU</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID><rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>{vcpus}</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
        <rasd:ElementName>{memory} MB RAM</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID><rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>{memory}</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:Address>0</rasd:Address>
        <rasd:ElementName>SCSI Controller 0</rasd:ElementName>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:ResourceSubType>VirtualSCSI</rasd:ResourceSubType>
        <rasd:ResourceType>6</rasd:ResourceType>
      </Item>
{nic_items}
{disk_items}
{efi_item}    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>""".encode()


def _manifest(entries: list[tuple[str, str]]) -> bytes:
    """OVA manifest: SHA256(filename)= hexhash per line."""
    return "".join(f"SHA256({name})= {digest}\n" for name, digest in entries).encode()


# ── Verify ────────────────────────────────────────────────────────────────────

def _verify_vmdk(vmdk: Path) -> None:
    log.info("Verifying %s", vmdk.name)
    r = subprocess.run(["qemu-img", "check", str(vmdk)], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    for line in out.splitlines():
        log.info("qemu-img: %s", line)
    if r.returncode != 0:
        raise SystemExit(f"VMDK check failed (rc={r.returncode}): {vmdk.name}\n{out}")
    log.info("OK: %s", vmdk.name)


# ── Progress ──────────────────────────────────────────────────────────────────

def _fmt_dur(s: float) -> str:
    s = int(s); h = s // 3600; m = (s % 3600) // 60; s = s % 60
    return f"{h}h {m:02d}m {s:02d}s" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def _make_progress(label: str, total: int) -> Callable[[int, int], None]:
    t0 = time.monotonic(); last_logged = [-1]; last_print = [0.0]

    def show(done: int, _total: int) -> None:
        now = time.monotonic(); dt = now - t0
        if now - last_print[0] < 1.0 and done < _total:
            return
        last_print[0] = now
        pct = done * 100 // _total if _total else 0
        if dt > 0:
            speed = done / dt / (1 << 20)
            eta   = _fmt_dur((_total - done) / (done / dt) if done else 0)
        else:
            speed = 0.0; eta = "--"
        print(f"\r  {pct:3d}%  {done >> 20}/{_total >> 20} MiB"
              f"  {speed:.1f} MiB/s  {_fmt_dur(dt)} elapsed  ETA {eta}   ",
              end="", flush=True)
        decile = pct // 10
        if decile != last_logged[0]:
            last_logged[0] = decile
            log.info("%s %d%%  %d/%d MiB  %.1f MiB/s  %s elapsed  ETA %s",
                     label, pct, done >> 20, _total >> 20, speed, _fmt_dur(dt), eta)
    return show


# ── main ──────────────────────────────────────────────────────────────────────

def _setup_logging(log_path: Path | None) -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    if log_path is not None:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG); fh.setFormatter(fmt); log.addHandler(fh)
        log.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING); sh.setFormatter(fmt); log.addHandler(sh)
    if log_path is None:
        log.setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Export Proxmox VM to OVA — parallel VMDK compression + streaming hole-punch")
    ap.add_argument("--vmid",    type=int,  required=True, metavar="ID",
                    help="Proxmox VM ID")
    ap.add_argument("--out",     type=Path, default=Path("/var/lib/vz/import"), metavar="DIR",
                    help="Output directory for final OVA (default: /var/lib/vz/import)")
    ap.add_argument("--tmp",     type=Path, default=None, metavar="DIR",
                    help="Directory for intermediate VMDKs (default: same as --out)")
    ap.add_argument("--name",    default="", metavar="NAME",
                    help="OVA base name (default: VM name)")
    ap.add_argument("--workers", type=int, default=min(32, os.cpu_count() or 4), metavar="N",
                    help="Parallel compression threads")
    ap.add_argument("--level",   type=int, default=1, choices=range(1, 10), metavar="1-9",
                    help="zlib compression level (1=fast/large … 9=slow/small)")
    ap.add_argument("--log",     type=Path, default=None, metavar="FILE",
                    help="Append detailed log to FILE")
    ap.add_argument("--verify",  action="store_true",
                    help="Run qemu-img check on each VMDK after conversion")
    args = ap.parse_args()

    _setup_logging(args.log)

    if os.geteuid() != 0:
        raise SystemExit("Must run as root")
    if _vm_status(args.vmid) != "stopped":
        raise SystemExit(f"VM {args.vmid} must be stopped before export")

    cfg = _vm_config(args.vmid)
    if not cfg["disks"]:
        raise SystemExit("No exportable disks found in VM config")

    if cfg["scsihw"].startswith("virtio"):
        msg = (f"WARNING: VM uses '{cfg['scsihw']}' — after import on Proxmox "
               f"change SCSI controller to 'VirtIO SCSI' in Hardware settings")
        print(msg)
        log.warning(msg)

    name = args.name or cfg["name"]
    if args.tmp is None:
        args.tmp = args.out
    args.out.mkdir(parents=True, exist_ok=True)
    args.tmp.mkdir(parents=True, exist_ok=True)
    ova_path = args.out / f"{name}.ova"

    print(f"\n{'='*60}")
    print(f"  VM {args.vmid} — {name}  ({cfg['vcpus']} vCPU, {cfg['memory']} MB, bios={cfg['bios']})")
    print(f"  Disks   : {' '.join(cfg['disks'])}")
    print(f"  Networks: {' '.join(cfg['networks']) or '(none detected)'}")
    print(f"  Output  : {ova_path}")
    print(f"  Workers : {args.workers}  level {args.level}  verify {'yes' if args.verify else 'no'}")
    print(f"{'='*60}\n")
    log.info("Export VM %d (%s) disks=%s workers=%d verify=%s",
             args.vmid, name, cfg["disks"], args.workers, args.verify)

    vmdk_paths: list[Path] = []

    def _cleanup(sig=None, _frame=None) -> None:
        for p in vmdk_paths:
            p.unlink(missing_ok=True)
        if sig is not None:
            log.warning("Interrupted (signal %s)", sig)
            sys.exit(1)

    signal.signal(signal.SIGINT,  _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)
    t_start = time.monotonic()

    try:
        disk_meta   = []
        vmdk_hashes = []  # (filename, sha256) collected during conversion for .mf

        for i, lv in enumerate(cfg["disks"]):
            vmdk    = args.tmp / f"{name}-disk{i}.vmdk"
            lv_path = f"/dev/pve/{lv}"
            vmdk_paths.append(vmdk)

            print(f"[{i}] {lv}  →  {vmdk.name}")
            log.info("[disk %d] %s → %s", i, lv, vmdk.name)
            t_disk = time.monotonic()
            _lv("y", lv)
            try:
                vsize    = int(subprocess.check_output(["blockdev", "--getsize64", lv_path]).strip())
                vmdk_sha = VmdkWriter(
                    src=lv_path, dst=vmdk, workers=args.workers, level=args.level,
                    on_progress=_make_progress(f"disk{i}", vsize),
                ).convert()
            finally:
                _lv("n", lv)

            elapsed   = time.monotonic() - t_disk
            vmdk_size = vmdk.stat().st_size
            print(f"\n  → {vmdk.name}  {vmdk_size >> 20} MiB"
                  f"  ({_fmt_dur(elapsed)}, {vsize / elapsed / (1 << 20):.1f} MiB/s)")
            log.info("[disk %d] done: %d MiB → %d MiB in %s",
                     i, vsize >> 20, vmdk_size >> 20, _fmt_dur(elapsed))

            if args.verify:
                print(f"  Verify {vmdk.name}...")
                _verify_vmdk(vmdk)
                print("  OK")

            disk_meta.append({"name": vmdk.name, "size": vmdk_size, "vsize": vsize})
            vmdk_hashes.append((vmdk.name, vmdk_sha))

        # Pack OVA: OVF → MF → VMDKs (manifest must precede disk files per OVF spec)
        ovf_data = _ovf(name, cfg["vcpus"], cfg["memory"], cfg["bios"], disk_meta, cfg["networks"])
        mf_data  = _manifest(
            [(f"{name}.ovf", hashlib.sha256(ovf_data).hexdigest())] + vmdk_hashes
        )

        print("\nPacking OVA (hole-punch streaming)...")
        log.info("Packing OVA: %s", ova_path)
        t_pack = time.monotonic()
        with OvaWriter(ova_path) as ova:
            ova.add_bytes(f"{name}.ovf", ovf_data)
            ova.add_bytes(f"{name}.mf",  mf_data)
            for vmdk in list(vmdk_paths):
                sz = vmdk.stat().st_size
                print(f"  {vmdk.name}  ({sz >> 20} MiB)")
                log.info("Packing %s (%d MiB)", vmdk.name, sz >> 20)
                t0 = time.monotonic()
                ova.add_file(vmdk.name, vmdk, on_progress=_make_progress(f"pack:{vmdk.stem}", sz))
                pack_t = time.monotonic() - t0
                print(f"\n  → packed {_fmt_dur(pack_t)}  ({sz / pack_t / (1 << 20):.1f} MiB/s)")
                log.info("Packed %s in %s", vmdk.name, _fmt_dur(pack_t))
                vmdk_paths.remove(vmdk)

        log.info("OVA packed in %s", _fmt_dur(time.monotonic() - t_pack))

    except Exception as exc:
        log.error("Fatal: %s", exc, exc_info=True)
        _cleanup()
        raise SystemExit(f"Error: {exc}") from exc

    elapsed = time.monotonic() - t_start
    gb      = ova_path.stat().st_size / 1024 ** 3
    print(f"\n{'='*60}")
    print(f"  {ova_path.name}  ({gb:.2f} GB)  {elapsed / 60:.1f} min")
    print(f"{'='*60}\n")
    log.info("Done: %s (%.2f GB) in %.1f min", ova_path.name, gb, elapsed / 60)


if __name__ == "__main__":
    main()
