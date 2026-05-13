# pvstream2ova

Export Proxmox VM to OVA with streaming hole-punch and parallel VMDK compression.

---

🇮🇹 [Versione italiana](#versione-italiana) — 🇬🇧 [English version](#english-version)

---

## English version

### Features

- **Parallel zlib**: each 64 KiB VMDK grain is compressed in a separate thread via `ThreadPoolExecutor` — default `min(32, cpu_count)` threads, ~476 MiB/s on a 100 GB disk
- **Hole-punch streaming**: the VMDK is hole-punched (`fallocate FALLOC_FL_PUNCH_HOLE`) as it is streamed into the OVA — peak disk usage equals the final OVA size, never more
- OVF 1.0 + SHA256 manifest (.mf) for VMware / VirtualBox / QNAP compatibility
- Automatic warnings: VirtIO SCSI controller, efidisk0 skip, BIOS OVMF
- Proxmox network bridge recorded in the OVF Description field
- `--log FILE` and `--verify` (qemu-img check after conversion)

### Benchmark

Real benchmark on a VM with a 100 GB disk (52 GB used), NVMe PCIe 4, read and write on the same device.

| Tool | VMDK | OVA packing | Total | Size | Portable |
|------|------|-------------|-------|------|---------|
| pvstream2ova `--level 6` *(default)* | 3:51 | 1:39 | **5:40** | 27,968 MiB | Yes (OVA) |
| pvstream2ova `--level 1` | 3:35 | 1:30 | 5:05 | 29,128 MiB | Yes (OVA) |
| pvstream2ova `--level 9` | 4:21 | 1:39 | 6:10 | 27,895 MiB | Yes (OVA) |
| qemu-img convert | 23:36 | — | 23:36 | 27,988 MiB | Yes (VMDK) |
| vzdump (zstd) | — | — | **2:31** | 25,214 MiB | No (Proxmox only) |

Notes:
- `qemu-img`: nearly single-threaded (CPU user ≈ real time), no OVA packing step
- `vzdump`: fastest and most compact but produces `.vma.zst`, importable on Proxmox only
- OVA packing = read VMDK + write OVA + hole-punch VMDK, all on the same NVMe → packing to a separate device would be faster

### VM prerequisites before export

- Disk on **local-lvm** (not local/QCOW2 — the script reads the LVM block device directly)
- Netplan without `match: macaddress` and without `set-name` — otherwise networking breaks after import (MAC address changes)
  ```bash
  sudo sed -i '/match:/,/macaddress:/d; /set-name:/d' /etc/netplan/00-installer-config.yaml
  sudo netplan apply
  ```

### Export workflow

```bash
# 0. Copy integrity scripts into the source VM (one-time)
scp /path/to/pvefingerprint.sh root@<VM-IP>:/root/
scp /path/to/ovafingerprint.sh root@<VM-IP>:/root/

# 1. Inside the source VM (as root)
sh /root/pvefingerprint.sh

# 2. Shut down the VM

# 3. From Proxmox
pvstream2ova --vmid <ID> --out /var/lib/vz/import/ --log /var/log/pvstream2ova-<ID>.log --verify
```

### Options

```
--vmid    ID      Proxmox VM ID to export (required)
--out     DIR     Output directory for the OVA (default: /var/lib/vz/import/)
--tmp     DIR     Directory for intermediate VMDKs (default: same as --out)
--name    NAME    OVA base name (default: VM name)
--workers N       Compression threads (default: min(32, cpu_count))
--level   1-9     zlib compression level (default: 6)
--log     FILE    Write detailed log to FILE
--verify          Run qemu-img check on each VMDK after conversion
```

### Import workflow on Proxmox GUI

**One-time prerequisite**: `Datacenter → Storage → local → Edit → Content → enable "Disk image"`

1. GUI → **Create VM → Import** → select OVA from `local → import`
2. In the wizard:
   - SCSI controller → **VirtIO SCSI**
   - Disk storage → **local-lvm**
   - Display → **Standard VGA** (not VMware Compatible — does not work with OVMF)
   - Network → map the bridge manually (the OVF Description shows the original bridge name)
3. Wait for **TASK OK**
4. Post-import for UEFI VMs (the wizard does not allow selecting the firmware):
   - **Hardware → BIOS → OVMF**
   - **Hardware → Add → EFI Disk → local-lvm** (uncheck **Pre-enroll keys**)
5. Check **Options → Boot Order** — scsi0 must be enabled
6. Start the VM
7. Inside the imported VM:
   ```bash
   sh /root/ovafingerprint.sh
   ```

### Known limitations (OVF standard)

| Issue | Cause | Post-import fix |
|-------|-------|----------------|
| LSI SCSI controller instead of VirtIO | VirtIO is not in the OVF vocabulary | Change to VirtIO SCSI in the wizard |
| SeaBIOS instead of OVMF | The import wizard does not support firmware selection | Hardware → BIOS → OVMF post-import |
| Network not configured | MAC address changes on import | Remove `match: macaddress` from netplan before export |
| Bridge must be mapped manually | OVF cannot enforce specific bridges | Bridge name is in the OVF Description |
| efidisk0 not exported | NVRAM is not portable across hypervisors | Add EFI Disk post-import (Ubuntu/Debian boot without NVRAM) |

### UEFI

- Works for full distros (Ubuntu, Debian): OVMF finds `\EFI\BOOT\BOOTX64.EFI` on the disk automatically
- Does not work for Alpine virt: the ISO does not support UEFI boot → no EFI partition on disk
- efidisk0 (NVRAM) always skipped: not portable across hypervisors

### Integrity scripts

#### pvefingerprint.sh
Run **inside the source VM** before export:
```bash
sh /root/pvefingerprint.sh
# Saves /root/.pvefingerprint
```
Captures: OS, filesystems, UUIDs, SHA256 of key files, file counts.
Excludes virtual filesystems: tmpfs, efivarfs, ramfs, devtmpfs, overlay.

#### ovafingerprint.sh
Run **inside the imported VM** after import:
```bash
sh /root/ovafingerprint.sh
# Reads /root/.pvefingerprint and compares
# Exit 0 if FAIL=0
```

### Validation results (2026-05-13)

| VM | Type | PASS | WARN | FAIL |
|----|------|------|------|------|
| Alpine BIOS (203→205) | BIOS, VirtIO SCSI | 12 | 0 | 0 ✅ |
| Ubuntu UEFI (204→205) | UEFI, VirtIO SCSI | 21 | 2* | 0 ✅ |

*Expected WARNs: gateway unreachable (temporary IP conflict with the source VM on the same network).
The efivarfs file count WARN appeared in the first validation run before the pvefingerprint.sh fix — not expected in subsequent exports.

### Storage requirements

- Only **local-lvm** (LVM thin pool) is supported
- local (QCOW2), ZFS, Ceph: not supported
- To move a disk from local to local-lvm: `qm move-disk <vmid> scsi0 local-lvm --delete`

---

## Versione italiana

### Caratteristiche

- **zlib parallelo**: ogni grain VMDK (64 KiB) viene compresso in un thread separato via `ThreadPoolExecutor` — default `min(32, cpu_count)` thread, ~476 MiB/s su disco da 100 GB
- **Hole-punch streaming**: il VMDK viene bucherellato (`fallocate FALLOC_FL_PUNCH_HOLE`) man mano che viene riversato nell'OVA — il picco di spazio disco occupato è uguale alla dimensione dell'OVA finale, mai di più
- OVF 1.0 + SHA256 manifest (.mf) per compatibilità VMware/VirtualBox/QNAP
- Warning automatici: VirtIO SCSI, efidisk0 skip, BIOS OVMF
- Network bridge Proxmox nella Description OVF
- `--log FILE` e `--verify` (qemu-img check post-conversione)

### Velocità vs alternative

Benchmark reale su VM con disco 100 GB (52 GB usati), NVMe PCIe 4, lettura e scrittura sullo stesso device.

| Tool | VMDK | OVA packing | Totale | Dimensione | Portabile |
|------|------|-------------|--------|-----------|----------|
| pvstream2ova `--level 6` *(default)* | 3:51 | 1:39 | **5:40** | 27,968 MiB | Sì (OVA) |
| pvstream2ova `--level 1` | 3:35 | 1:30 | 5:05 | 29,128 MiB | Sì (OVA) |
| pvstream2ova `--level 9` | 4:21 | 1:39 | 6:10 | 27,895 MiB | Sì (OVA) |
| qemu-img convert | 23:36 | — | 23:36 | 27,988 MiB | Sì (VMDK) |
| vzdump (zstd) | — | — | **2:31** | 25,214 MiB | No (solo Proxmox) |

Note:
- `qemu-img`: quasi single-thread (CPU user ≈ real time), nessun OVA packing
- `vzdump`: più veloce e compatto ma produce `.vma.zst`, importabile solo su Proxmox
- OVA packing = lettura VMDK + scrittura OVA + hole-punch sul VMDK, tutto sullo stesso NVMe → il packing su device separato sarebbe più veloce

### Prerequisiti VM prima dell'export

- Disco su **local-lvm** (non local/QCOW2 — lo script legge block device LVM direttamente)
- Netplan senza `match: macaddress` e senza `set-name` — altrimenti la rete non funziona post-import
  ```bash
  sudo sed -i '/match:/,/macaddress:/d; /set-name:/d' /etc/netplan/00-installer-config.yaml
  sudo netplan apply
  ```

### Workflow export

```bash
# 0. Copia gli script nella VM sorgente (una tantum)
scp /path/to/pvefingerprint.sh root@<VM-IP>:/root/
scp /path/to/ovafingerprint.sh root@<VM-IP>:/root/

# 1. Dentro la VM sorgente (come root)
sh /root/pvefingerprint.sh

# 2. Spegni la VM

# 3. Da Proxmox
pvstream2ova --vmid <ID> --out /var/lib/vz/import/ --log /var/log/pvstream2ova-<ID>.log --verify
```

### Opzioni

```
--vmid    ID      VM Proxmox da esportare (obbligatorio)
--out     DIR     Directory output OVA (default: /var/lib/vz/import/)
--tmp     DIR     Directory VMDK temporanei (default: uguale a --out)
--name    NAME    Nome base OVA (default: nome VM)
--workers N       Thread di compressione (default: min(32, cpu_count))
--level   1-9     Livello compressione zlib (default: 6)
--log     FILE    Log dettagliato su file
--verify          Esegue qemu-img check su ogni VMDK
```

### Workflow import su Proxmox GUI

**Prerequisito una tantum**: `Datacenter → Storage → local → Edit → Content → aggiungi "Disk image"`

1. GUI → **Create VM → Import** → seleziona OVA da `local → import`
2. Nel wizard:
   - SCSI controller → **VirtIO SCSI**
   - Disk storage → **local-lvm**
   - Display → **Standard VGA** (non VMware Compatible — non funziona con OVMF)
   - Network → mappare manualmente il bridge (la Description OVF mostra il bridge originale)
3. Aspetta **TASK OK**
4. Post-import per VM UEFI (il wizard non permette di scegliere BIOS):
   - **Hardware → BIOS → OVMF**
   - **Hardware → Add → EFI Disk → local-lvm** (deseleziona **Pre-enroll keys**)
5. Verifica **Options → Boot Order** — scsi0 deve essere abilitato
6. Avvia VM
7. Dentro la VM importata:
   ```bash
   sh /root/ovafingerprint.sh
   ```

### Limitazioni note (OVF standard)

| Problema | Causa | Fix post-import |
|---------|-------|----------------|
| SCSI controller LSI invece di VirtIO | VirtIO non è nello standard OVF | Cambiare in VirtIO SCSI nel wizard |
| BIOS SeaBIOS invece di OVMF | Il wizard non supporta selezione BIOS | Hardware → BIOS → OVMF post-import |
| Network non configurata | MAC address cambia all'import | Rimuovere `match: macaddress` dal netplan prima dell'export |
| Rete da mappare manualmente | OVF non può imporre bridge specifici | Il nome bridge è nella Description OVF |
| efidisk0 non esportato | NVRAM non portabile tra hypervisor | Aggiungere EFI Disk post-import (Ubuntu/Debian bootano senza NVRAM) |

### UEFI

- Funziona per distro full (Ubuntu, Debian): OVMF trova `\EFI\BOOT\BOOTX64.EFI` sul disco automaticamente
- Non funziona per Alpine virt: l'ISO non supporta boot UEFI → nessuna EFI partition sul disco
- efidisk0 (NVRAM) sempre skippato: non portabile tra hypervisor

### Script di integrità

#### pvefingerprint.sh
Da eseguire **dentro la VM sorgente** prima dell'export:
```bash
sh /root/pvefingerprint.sh
# Salva /root/.pvefingerprint
```
Cattura: OS, filesystem, UUID, SHA256 file chiave, conteggio file.
Esclude filesystem virtuali: tmpfs, efivarfs, ramfs, devtmpfs, overlay.

#### ovafingerprint.sh
Da eseguire **dentro la VM importata** dopo l'import:
```bash
sh /root/ovafingerprint.sh
# Legge /root/.pvefingerprint e confronta
# Exit 0 se FAIL=0
```

### Risultati validazione (13/05/2026)

| VM | Tipo | PASS | WARN | FAIL |
|----|------|------|------|------|
| Alpine BIOS (203→205) | BIOS, VirtIO SCSI | 12 | 0 | 0 ✅ |
| Ubuntu UEFI (204→205) | UEFI, VirtIO SCSI | 21 | 2* | 0 ✅ |

*WARN attesi: gateway irraggiungibile (IP conflict temporaneo con VM sorgente attiva sulla stessa rete).
Il WARN efivarfs (file count) era presente nella prima validazione prima del fix a pvefingerprint.sh — non atteso nelle esportazioni successive.

### Storage requirements

- Solo **local-lvm** (LVM thin pool) supportato
- local (QCOW2), ZFS, Ceph: non supportati
- Per disco su local: `qm move-disk <vmid> scsi0 local-lvm --delete`
