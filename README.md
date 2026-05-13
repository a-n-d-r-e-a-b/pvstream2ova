# pvstream2ova

Export Proxmox VM to OVA with streaming hole-punch and parallel VMDK compression.

## Caratteristiche

- **zlib parallelo**: ogni grain VMDK (64 KiB) viene compresso in un thread separato via `ThreadPoolExecutor` — default `min(32, cpu_count)` thread, ~476 MiB/s su disco da 100 GB
- **Hole-punch streaming**: il VMDK viene bucherellato (`fallocate FALLOC_FL_PUNCH_HOLE`) man mano che viene riversato nell'OVA — il picco di spazio disco occupato è uguale alla dimensione dell'OVA finale, mai di più
- OVF 1.0 + SHA256 manifest (.mf) per compatibilità VMware/VirtualBox/QNAP
- Warning automatici: VirtIO SCSI, efidisk0 skip, BIOS OVMF
- Network bridge Proxmox nella Description OVF
- `--log FILE` e `--verify` (qemu-img check post-conversione)

## Velocità vs alternative

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

## Prerequisiti VM prima dell'export

- Disco su **local-lvm** (non local/QCOW2 — lo script legge block device LVM direttamente)
- Netplan senza `match: macaddress` e senza `set-name` — altrimenti la rete non funziona post-import
  ```bash
  sudo sed -i '/match:/,/macaddress:/d; /set-name:/d' /etc/netplan/00-installer-config.yaml
  sudo netplan apply
  ```

## Workflow export

```bash
# 0. Copia gli script nella VM sorgente (una tantum)
scp /projects/pvstream2ova/pvefingerprint.sh root@<VM-IP>:/root/
scp /projects/pvstream2ova/ovafingerprint.sh root@<VM-IP>:/root/

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

## Workflow import su Proxmox GUI

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

## Limitazioni note (OVF standard)

| Problema | Causa | Fix post-import |
|---------|-------|----------------|
| SCSI controller LSI invece di VirtIO | VirtIO non è nello standard OVF | Cambiare in VirtIO SCSI nel wizard |
| BIOS SeaBIOS invece di OVMF | Il wizard non supporta selezione BIOS | Hardware → BIOS → OVMF post-import |
| Network non configurata | MAC address cambia all'import | Rimuovere `match: macaddress` dal netplan prima dell'export |
| Rete da mappare manualmente | OVF non può imporre bridge specifici | Il nome bridge è nella Description OVF |
| efidisk0 non esportato | NVRAM non portabile tra hypervisor | Aggiungere EFI Disk post-import (Ubuntu/Debian bootano senza NVRAM) |

## UEFI

- Funziona per distro full (Ubuntu, Debian): OVMF trova `\EFI\BOOT\BOOTX64.EFI` sul disco automaticamente
- Non funziona per Alpine virt: l'ISO non supporta boot UEFI → nessuna EFI partition sul disco
- efidisk0 (NVRAM) sempre skippato: non portabile tra hypervisor

## Script di integrità

### pvefingerprint.sh
Da eseguire **dentro la VM sorgente** prima dell'export:
```bash
sh /root/pvefingerprint.sh
# Salva /root/.pvefingerprint
```
Cattura: OS, filesystem, UUID, SHA256 file chiave, conteggio file.
Esclude filesystem virtuali: tmpfs, efivarfs, ramfs, devtmpfs, overlay.

### ovafingerprint.sh
Da eseguire **dentro la VM importata** dopo l'import:
```bash
sh /root/ovafingerprint.sh
# Legge /root/.pvefingerprint e confronta
# Exit 0 se FAIL=0
```

## Risultati validazione (13/05/2026)

| VM | Tipo | PASS | WARN | FAIL |
|----|------|------|------|------|
| Alpine BIOS (203→205) | BIOS, VirtIO SCSI | 12 | 0 | 0 ✅ |
| Ubuntu UEFI (204→205) | UEFI, VirtIO SCSI | 21 | 2* | 0 ✅ |

*WARN attesi: gateway irraggiungibile (IP conflict temporaneo con VM sorgente attiva sulla stessa rete).
Il WARN efivarfs (file count) era presente nella prima validazione prima del fix a pvefingerprint.sh — non atteso nelle esportazioni successive.

## Storage requirements

- Solo **local-lvm** (LVM thin pool) supportato
- local (QCOW2), ZFS, Ceph: non supportati
- Per disco su local: `qm move-disk <vmid> scsi0 local-lvm --delete`
