# Proxmox setup and operation

This adapter is separate from the upstream workflow. Start with the
[design and isolation model](proxmox-design.md). The repository name has not
been changed; `lfs-proxmox` names the deployment components.

Validated in the reference environment: isolated VM lifecycle, blocked host
connections and working public HTTPS, the complete upstream `part2` preparation
stage, checkpoint save, and restoration/import/mount in a fresh 12-vCPU VM on
SSD-backed `local-lvm`. The test VMs were removed. The full LFS compilation and
final boot test have not yet completed validation through the adapter. A repository-level
Mac runner is installed and online in the reference environment; no organization
runner was changed. The workflow is published, its GitHub Actions smoke run
passed, and the first full build is in progress. See the [validation status](proxmox-design.md#implementation-status).

## Controller requirements

Use Python 3.10 or newer, Git, and OpenSSH on the local Mac runner. Install the
controller dependency in an isolated environment:

```sh
python3 -m venv scripts/proxmox/.venv
scripts/proxmox/.venv/bin/pip install -r scripts/proxmox/requirements.txt
scripts/proxmox/.venv/bin/python scripts/proxmox/run.py --mode plan
scripts/proxmox/.venv/bin/python -m unittest discover -s tests/proxmox -v
```

`plan` does not contact Proxmox. The adapter validates the supported actions,
conditions, dependency order, and shell format before execution. If upstream
introduces a new form, it stops for review instead of guessing.

## One-time Proxmox administrator setup

Prepare an updated Ubuntu 24.04 template with cloud-init, the QEMU guest agent,
Docker, Compose, QEMU, Expect, Git, and curl. Generalize its identity and keep
its network disconnected. The reference template is 9001.

Choose an unused bridge and private /24, an unused builder ID, a storage pool,
and a directory with ample free space for checkpoints. The installer defaults
to bridge `vmbr99`, subnet `172.30.240.0/24`, template 9001, and builder 9100.
The bridge must have no physical uplink or other VMs. Do not reuse an existing
bridge. The uplink must already have per-interface IPv4 forwarding enabled;
the installer refuses to change the global forwarding switch.

Generate a dedicated runner key **outside the Git checkout**:

```sh
mkdir -p "$HOME/.config/lfs-proxmox"
ssh-keygen -t ed25519 -N '' -f "$HOME/.config/lfs-proxmox/runner_key"
```

Copy `scripts/proxmox/broker.py`, `scripts/proxmox/install-host.py`, and only
the runner's **public** key to an administrator-owned setup directory on
Proxmox. Then run the installer on Proxmox as root; adjust these example
storage/interface values for your server:

```sh
python3 /root/lfs-proxmox-setup/install-host.py \
  --uplink vmbr0 \
  --cores 12 \
  --storage local-lvm \
  --space-path /data \
  --state-dir /data/lfs-proxmox \
  --runner-public-key /root/lfs-proxmox-setup/runner_key.pub
```

The reference deployment puts active builder disks on SSD-backed `local-lvm`
and keeps checkpoints under `/data/lfs-proxmox` on HDD-backed `data-zfs`.
VM-pool capacity is checked through Proxmox's storage API, separately from
checkpoint filesystem capacity. These names and paths are deployment choices.
The reference VM uses 12 vCPUs. Upstream still selects serial compilation for
some early stages and `nproc` in the final chroot; the adapter does not override
those build settings just because more vCPUs are available.

The installer creates only its own bridge, nftables table, network service,
configuration directory, and broker. It appends a forced-command, restricted
key to root's authorized keys without changing existing administrator keys or
sshd settings. This key cannot open a shell, allocate a TTY, forward ports, or
forward an agent. The controller explicitly disables SSH-agent use and ignores
the user's SSH configuration so it cannot fall back to a broader admin key.

The broker runs with the privileges needed to manage Proxmox, but accepts only
its defined JSON operations. Its VM ID and template come from root-owned
configuration, never workflow input. Ownership checks protect other VMs and
other runs. Treat the broker and installer as trusted administrator code:
review changes before installing an updated version on Proxmox.

Its private guest-login key stays on Proxmox. The guest receives only its public
key. The build has no Proxmox credentials, host directory mounts, or forwarded
SSH agent. Guest archives are stored as opaque files on Proxmox and are never
extracted there. Checkpoints and disk transfers are limited to 32 GiB each;
other artifacts are limited to 2 GiB. Space checks budget for the incoming VM
or checkpoint while retaining 100 GiB host headroom, and for incoming artifacts
while retaining 10 GiB on the controller.

## Smoke test and build

Set deployment values locally, outside public source files. Verify the Proxmox
SSH host key through your normal administrator connection first; the adapter
requires an already trusted host key and does not disable verification.

```sh
export LFS_PROXMOX_HOST=pve.example
export LFS_PROXMOX_KEY_PATH="$HOME/.config/lfs-proxmox/runner_key"
export LFS_PROXMOX_ARTIFACTS="$HOME/lfs-artifacts"
scripts/proxmox/.venv/bin/python scripts/proxmox/run.py --mode smoke
```

The smoke test creates a full clone, waits for cloud-init, verifies blocked SSH
connections to the host's bridge and LAN addresses, tests public HTTPS, checks
installed tools, and destroys the clone. Cloud-init deprecation warnings alone
do not count as initialization failures. Actual cloud-init errors do.

Once the smoke test succeeds, commit the reviewed checkout before building:

```sh
scripts/proxmox/.venv/bin/python scripts/proxmox/run.py --mode build
```

The adapter transfers a `git archive` of that commit, without Git credentials,
and executes the six upstream stages in their existing order. Each gets a
fresh VM. The same Docker save/load, image checkpoints, build commands, test
shortcuts, timeouts, and pseudo-terminal behavior are retained. For deployment
purposes only:

- GitHub-hosted-runner disk cleanup is skipped on the dedicated VM.
- Checkout is supplied through the source archive.
- Cache keys and save/restore boundaries are mapped to local Proxmox storage.
  Writes occur after a successful stage, matching actions/cache post-job timing.
- Artifact actions copy files to local controller storage.
- Git identity setup and report commit/force-push steps are skipped. The adapter
  has no repository write credentials and does not publish changes.

These adaptations do not edit the upstream workflow. Review the adapter when
upstream changes the workflow. It does not add new LFS pins, fix book commands,
or alter the upstream test policy.

The default artifact location is `~/.local/share/lfs-proxmox/artifacts/RUN-ATTEMPT`,
outside the checkout so a later Actions checkout cannot erase the disk image.
The final `build_dir.img` is independent of the deleted builder. Intermediate
checkpoints remain under the administrator's configured state directory on
Proxmox. They consume space across runs; review and remove obsolete checkpoints
as an administrator. The broker does not grant the runner arbitrary host-file
deletion privileges.

To resume a failed stage, check out the same repository commit, reuse its printed
run ID, and select that stage; the preceding stage's checkpoints must exist.
Subsequent stages can be invoked
with the same run ID. Use a new run attempt when intentionally rebuilding a
completed stage, since checkpoints are immutable:

```sh
scripts/proxmox/.venv/bin/python scripts/proxmox/run.py \
  --mode build --run 123456-1 --stage part3luser
```

If a failure interrupted checkpoint saving, a new full run is the simplest
recovery. An administrator can also review the incomplete checkpoint set;
the adapter does not silently mix old and new checkpoints or overwrite them.

## GitHub Actions runner

Use the [README installation steps](../README.md#install-the-local-proxmox-runner)
and `scripts/proxmox/install-runner.py` to install a separate repository runner
without changing an organization runner. The installer verifies the official
release checksum, registers the runner, starts its macOS user service, and can
configure the deployment variables below.

The separate `.github/workflows/linux-proxmox.yaml` is manual-only. It needs a trusted
local macOS runner with the label `lfs-proxmox`. Add repository Actions variables:

| Variable | Value |
| --- | --- |
| `LFS_PROXMOX_HOST` | Private Proxmox hostname or IPv4 address |
| `LFS_PROXMOX_KEY_PATH` | Absolute path to the restricted private key on the runner |
| `LFS_PROXMOX_ARTIFACTS` | Optional absolute local artifact directory |

Do not put the private key in Git, transfer it to a guest, or use an unrestricted
administrator key for this workflow. Run the workflow in `smoke` mode first,
then select `build`. One controller job orchestrates the six upstream stage
boundaries and a fresh VM for each; concurrent builds are queued.

A personal repository needs its own repository-level runner registration. An
organization runner is available only within its allowed organization scope.
Separate registrations can coexist in separate directories on the same Mac,
but processes under the same macOS user are not a security boundary. Keep this
runner limited to trusted manual workflow revisions; do not enable public fork
pull-request execution on it. See [GitHub runner setup](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).

The original upstream workflow and its triggers are unchanged. It will still
target GitHub-hosted runners if enabled; use **linux-proxmox** for this adapter.

## Cleanup and recovery

The controller cleans up in `finally`, handles termination, and has an Actions
`always()` cleanup step. Repeat a matching cleanup when recovering from a
controller crash or lost connection:

```sh
scripts/proxmox/.venv/bin/python scripts/proxmox/run.py \
  --mode cleanup --run 123456-1
```

Cleanup refuses to delete another run's VM. A failed or interrupted creation,
changed ownership marker, changed network configuration, or unexpected host
access in the VM configuration can require administrator investigation. Never
work around a refusal by deleting an unrelated VM or disabling the guard.

After a host restart, the network service restores only this bridge's rules.
The broker checks the ruleset fingerprint and bridge membership before running
guest commands. Do not attach other VMs to this bridge. If the firewall or
bridge configuration is intentionally changed, review it and revalidate the
isolation before updating the trusted fingerprint.
