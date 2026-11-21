# Proxmox deployment design

Build the upstream jhalfs project on Proxmox, producing its persistent, bootable
systemd LFS disk image. The repository name is unchanged; `lfs-proxmox` names
the deployment components.

**This deployment requires Proxmox VE and a self-hosted GitHub Actions runner
on your local network.** The reference controller is a MacBook. GitHub-hosted
runners cannot reach this private deployment.

The separate adapter and manual workflow are implemented. VM provisioning,
isolation, tool execution, and cleanup passed a smoke test in the reference
environment. The first upstream preparation stage and checkpoint handoff also
passed, including restore/load/mount in a fresh SSD-backed, 12-vCPU clone. A
complete LFS build has not yet been validated. See the [setup runbook](proxmox-runbook.md).

## Architecture

```text
GitHub Actions
    |
    v
Trusted self-hosted runner on the local network (reference: MacBook)
    | dedicated forced-command SSH key; no arbitrary host shell
    v
Proxmox: root-owned broker restricted to one builder VM slot
    |
    +-- Ubuntu 24.04 base cloud-init template (example: 9000)
    |       +-- Updated builder template (example: 9001)
    |               +-- Fresh VM for each upstream stage (example slot: 9100)
    |                       +-- Privileged Docker: jhalfs/LFS
    |                       +-- QEMU: final image boot test
    |
    +-- Dedicated bridge, with no physical uplink or other VMs
    |       +-- Bridge-scoped rules: public Internet allowed, host/LAN blocked
    |
    +-- Other VMs: their configuration and network remain unchanged
```

Site-specific hostnames, addresses, key paths, and storage settings belong in
private deployment configuration, not public source files. The resulting LFS
image is separate from the Ubuntu builder template.

## Update once, clone for each stage

Clone the original cloud-init template into a maintenance VM, apply Ubuntu
updates, install Docker, Compose, QEMU, Expect, Git, curl, tmux, CA certificates,
and the QEMU guest agent, then reboot and validate. Save a package/tool manifest.

Generalize machine identity and cloud-init state, remove inherited login keys
and SSH host keys, remove one-time maintenance user data, and convert the
shut-down VM into a new cloud-init template. Leave its adapter disconnected.

The broker replaces that adapter with verified private-network settings on each
full clone. Tools and updates in the template are reused. New template versions
should be validated before replacing the working one. Full clones are the
initial design; linked clones and their dependency management are optional
future work.

The reference build VM has 12 vCPUs, 16 GiB RAM, a 100 GiB disk, CPU limits,
100 MB/s and 20,000 IOPS disk limits. The private VM link is not rate-policed,
so host-to-guest checkpoint transfers do not suffer policing losses. These are
deployment choices, not measured LFS minimums. Checkpoints require substantial
additional host storage and a free-space reserve.

In the reference deployment, active VM disks use SSD-backed `local-lvm`.
Opaque checkpoints use HDD-backed `data-zfs` through `/data/lfs-proxmox`.
The broker checks the VM pool and checkpoint filesystem separately.

## Preserve upstream phases

LFS has cross-toolchain, temporary-tool, final chroot, and boot-configuration
phases. It does not require killing a VM between phases. A chroot changes the
process root directory; it does not create a new VM.

Upstream splits those phases into six jobs to accommodate GitHub limits.
**Keep those stages, checkpoints, test shortcuts, and workarounds.** The adapter
reads the unchanged workflow and follows:

`part2 → part3luser → part3shadow → part3gcc → part3remaining → part4`

For each stage it clones a VM, waits for initialization, checks network isolation,
transfers the committed source, restores required state, executes the upstream
commands, saves successful checkpoints, collects artifacts, and destroys the VM.
The next stage restores the container, disk image, and metadata it needs; it
does not continue from an empty filesystem.

GitHub checkout/cache/artifact transport is adapted to source archives and local
storage. The hosted-runner disk cleanup and report commit/force-push steps are
not executed by the adapter. The LFS commands and test policy remain unchanged.
The original GitHub workflow, including its triggers and publication behavior,
is preserved byte-for-byte.

Cleanup checks the run/stage ownership marker. It must never delete a different
run's VM or other infrastructure. Failed controller connections or partial
creation can require administrator recovery; see the runbook.

## Isolation

Treat a privileged-container compromise as compromise of the entire build VM.
The VM is the security boundary.

- Build VMs have dedicated virtual disks, no physical-device passthrough, no
  host shares, no network storage mounts, and no Proxmox or GitHub credentials.
- The dedicated bridge has no physical uplink. Only the designated builder is
  permitted on it. Anti-spoofing rules allow only its assigned source address.
- Administrator-installed nftables rules block new guest connections to the
  host and to private/reserved IPv4 destinations, and block IPv6. Established
  replies to administrator-initiated guest SSH sessions are allowed.
- NAT allows public IPv4 downloads through the existing uplink. No global
  forwarding switch or Proxmox firewall setting is changed. Existing VM
  networks are not reloaded.
- Before guest operations, the broker verifies the ruleset fingerprint, bridge
  address, disabled bridge IPv6, and expected bridge membership.
- A dedicated forced-command key can invoke only the broker's defined
  operations. It cannot open a host shell or forward ports or an SSH agent.
  The broker's private guest key stays on Proxmox.
- Guest checkpoints are opaque host files; they are never extracted on Proxmox.
  Transfer-size limits and free-space checks protect artifact storage.
- The Mac runner executes trusted manual workflow revisions only. Public fork
  pull requests must not execute on it. Separate runner processes under one
  macOS user do not isolate their credentials.

Keep Proxmox and guests updated. These controls reduce risk; they do not
guarantee protection against hypervisor/kernel vulnerabilities or every form
of resource exhaustion.

## Book updates, systemd, and persistence

The adapter preserves upstream's `ubuntu:latest`, jhalfs selection, book
`COMMIT="trunk"`, and test settings. Preparing a new build follows newer book
revisions; an already prepared build does not update itself. jhalfs generates
package instructions from the book, but changes can still require maintenance
of jhalfs, dependencies, or project workarounds.

The new Proxmox workflow is manual-only. Upstream's scheduled/push workflow is
unchanged. Pinning tested revisions or adding scheduled Proxmox compatibility
jobs is an optional future policy, not a default change introduced by this fork.

The output is `build_dir.img`, with a partitioned ext4 filesystem and BIOS GRUB.
Changes made while running that disk can persist across reboots. This project
selects the systemd book through `BOOK_LFS_SYSD=y` and `INITSYS="systemd"`.

It does not currently generate an ISO. The older sister project used LFS 8.2,
SysVinit, and ISOLINUX packaging with a RAM-based root filesystem. Proxmox is
required by this supported orchestration, not intrinsically by the built Linux
system; other hypervisors have not been validated here.

The upstream QEMU boot test uses software emulation (`tcg`), so nested hardware
virtualization is not required. The adapter does not silently fix upstream boot
settings or test failures; general-purpose fixes should be separately reviewable.

## Implementation status

| Component | Status |
| --- | --- |
| Upstream scripts, configuration, workflow | Unchanged |
| Updated/generalized Ubuntu 24.04 template | Created; clone initialization passed |
| Bridge isolation and restricted SSH broker | Installed and smoke-tested |
| Provisioning, tool execution, cleanup, resource limits | Smoke-tested |
| Six-stage adapter and separate manual workflow | Implemented; first stage and handoff to the next stage passed |
| Local artifact/checkpoint transport | Full-size checkpoint save/restore passed; final image export not yet validated |
| Personal-repository GitHub runner | Installed as a separate macOS LaunchAgent and verified online; organization runner untouched |
| Full LFS build and final boot test on this adapter | Not yet validated |

The original [README](../README.md) retains upstream's Docker instructions.
Follow the [Proxmox runbook](proxmox-runbook.md) for this deployment. Keep
Proxmox additions in separate files so upstream pulls and contributions remain
manageable.

References: [LFS](https://www.linuxfromscratch.org/lfs/),
[jhalfs](https://www.linuxfromscratch.org/alfs/),
[Proxmox cloud-init](https://pve.proxmox.com/wiki/Cloud-Init_Support).
The existing [license](../LICENSE) remains in place.
