#!/usr/bin/env python3
"""Root-owned, forced-SSH-command broker. Never runs build commands on Proxmox."""
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

CONFIG = Path('/etc/lfs-proxmox/config.json')
STAGES = ('part2', 'part3luser', 'part3shadow', 'part3gcc', 'part3remaining', 'part4')
CACHE_PATHS = {'jhalfs', 'jhalfs.tar', 'build_dir.img'}
WORK = '/home/ubuntu/lfs-cicd'


def hardware(vm, vmid):
    """Accept only this template's two owned virtual disks and virtual devices."""
    forbidden = ('hostpci', 'virtiofs', 'args', 'hookscript', 'usb', 'parallel', 'unused')
    if any(k.startswith(forbidden) for k in vm):
        raise ValueError('unexpected host access or extra disk in VM configuration')
    for key, value in vm.items():
        if re.fullmatch(r'(scsi|virtio|sata|ide)[0-9]+', key) and key not in {'scsi0', 'ide2'}:
            raise ValueError('unexpected extra virtual disk')
        if key.startswith('serial') and value != 'socket':
            raise ValueError('host serial passthrough is not allowed')
    for key, suffix in (('scsi0', r'disk-[0-9]+'), ('ide2', 'cloudinit')):
        volume = vm.get(key, '').split(',')[0]
        if ':' not in volume or volume.startswith('/') or not re.search(
            rf'(?:^|/)(?:base|vm)-{int(vmid)}-{suffix}(?:\.(?:raw|qcow2))?$', volume.split(':', 1)[1]
        ):
            raise ValueError(f'{key} is not an owned Proxmox virtual volume')


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(args):
    return command(args, stdout=subprocess.PIPE, text=True).stdout


def request(raw):
    if len(raw) > 200000:
        raise ValueError('request too large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('request must be an object')
    allowed = {'health', 'create', 'ready', 'verify', 'upload', 'exec', 'save', 'restore', 'cleanup',
               'artifact', 'destroy'}
    if not isinstance(value.get('op'), str) or value['op'] not in allowed:
        raise ValueError('unknown operation')
    fields = {'op', 'run', 'stage'}
    fields |= {'exec': {'script', 'env', 'timeout'}, 'save': {'key', 'path'},
               'restore': {'key', 'path'}, 'artifact': {'name'}}.get(value['op'], set())
    if set(value) - fields:
        raise ValueError('unsupported request fields; resources are fixed by the administrator')
    if value['op'] != 'health':
        if not isinstance(value.get('run'), str) or not re.fullmatch(
            r'[0-9]{1,20}-[0-9]{1,5}', value['run']
        ):
            raise ValueError('run must be GitHub run ID-attempt')
        if value.get('stage') not in STAGES:
            raise ValueError('unknown upstream stage')
    if value['op'] in {'save', 'restore'}:
        if not isinstance(value.get('path'), str) or value['path'] not in CACHE_PATHS:
            raise ValueError('unsupported cache path')
        if not isinstance(value.get('key'), str) or not re.fullmatch(
            r'[A-Za-z0-9_.-]{1,180}', value['key']
        ):
            raise ValueError('invalid cache key')
    if value['op'] == 'artifact':
        if not isinstance(value.get('name'), str) or not re.fullmatch(
            r'[A-Za-z0-9_.-]{1,120}', value['name']
        ):
            raise ValueError('invalid artifact name')
        if value['name'] in {'.', '..'}:
            raise ValueError('invalid artifact name')
    if value['op'] == 'exec':
        if not isinstance(value.get('script'), str) or len(value['script']) > 100000:
            raise ValueError('invalid step script')
        env = value.get('env', {})
        if not isinstance(env, dict) or any(
            not re.fullmatch(r'[A-Z][A-Z0-9_]*', k) or not isinstance(v, str)
            for k, v in env.items()
        ):
            raise ValueError('invalid guest environment')
        timeout = value.get('timeout', 21600)
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ValueError('invalid timeout')
    return value


class Broker:
    def __init__(self, config):
        self.c = config
        self.root = Path(config['state_dir'])
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state = self.root / 'active.json'
        self.vmid = str(config['vmid'])
        if int(self.vmid) == int(config['template']) or int(self.vmid) < 9000:
            raise ValueError('builder ID must be >=9000 and differ from template')
        self.node = output(['hostname']).strip()

    def vmconfig(self):
        return json.loads(output(['pvesh', 'get',
            f'/nodes/{self.node}/qemu/{self.vmid}/config', '--output-format', 'json']))

    def guard(self, r):
        active = json.loads(self.state.read_text())
        if (active['run'], active['stage']) != (r['run'], r['stage']):
            raise ValueError('VM is owned by another run or stage')
        vm = self.vmconfig()
        if vm.get('name') != active['name'] or vm.get('template'):
            raise ValueError('VM ownership mismatch; refusing operation')
        if vm.get('description') != f"lfs-proxmox:{r['run']}:{r['stage']}":
            raise ValueError('VM ownership marker changed')
        hardware(vm, self.vmid)
        if f"bridge={self.c['bridge']}" not in vm.get('net0', '').split(','):
            raise ValueError('builder is attached to the wrong network')
        if any(re.fullmatch(r'net[1-9][0-9]*', k) for k in vm):
            raise ValueError('unexpected extra network interface')
        return active

    def network(self):
        bridge = self.c['bridge']
        nft = output(['nft', '-j', 'list', 'table', 'inet', 'lfs_proxmox'])
        expected = Path('/etc/lfs-proxmox/nft.sha256').read_text().strip()
        # JSON output has counters/handles: rules use no counters and handles
        # are removed to keep this fingerprint stable across reloads.
        if fingerprint(nft) != expected:
            raise ValueError('isolation rules changed; refusing to run builds')
        addresses = json.loads(output(['ip', '-j', 'address', 'show', 'dev', bridge]))
        if not addresses or not any(
            a.get('local') == self.c['gateway'] for a in addresses[0]['addr_info']
        ):
            raise ValueError('build bridge address missing')
        if Path(f'/proc/sys/net/ipv6/conf/{bridge}/disable_ipv6').read_text().strip() != '1':
            raise ValueError('IPv6 must be disabled on the isolated bridge')
        # No physical/other VM ports may be members of this bridge.
        for port in Path(f'/sys/class/net/{bridge}/brif').iterdir():
            if port.name not in {f'tap{self.vmid}i0'}:
                raise ValueError(f'unexpected bridge member: {port.name}')

    def ssh_args(self, remote):
        return ['ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'IdentityAgent=none',
                '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
                '-o', f'UserKnownHostsFile={self.root / "known_hosts"}',
                '-i', str(self.root / 'guest_key'), f'ubuntu@{self.c["address"]}', remote]
    def ssh(self, remote, **kwargs):
        return command(self.ssh_args(remote), **kwargs)

    def receive(self, remote, target, maximum=32 * 2**30):
        proc = subprocess.Popen(self.ssh_args(remote), stdout=subprocess.PIPE)
        total = 0
        try:
            while True:
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ValueError('guest output exceeded the artifact/checkpoint size limit')
                target.write(chunk)
            if proc.wait() != 0:
                raise ValueError('guest transfer failed')
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def create(self, r):
        self.network()
        if self.state.exists() or Path(f'/etc/pve/qemu-server/{self.vmid}.conf').exists():
            raise ValueError('builder slot is occupied; use matching destroy or investigate')
        template = json.loads(output(['pvesh', 'get',
            f'/nodes/{self.node}/qemu/{self.c["template"]}/config', '--output-format', 'json']))
        if not template.get('template'):
            raise ValueError('source is not a template')
        hardware(template, self.c['template'])
        if 'cicustom' in template:
            raise ValueError('template has unexpected custom initialization')
        required = (self.c['min_free_gib'] + self.c['disk_gib']) * 2**30
        storage = json.loads(output(['pvesh', 'get',
            f'/nodes/{self.node}/storage/{self.c["storage"]}/status', '--output-format', 'json']))
        if not storage.get('active') or storage.get('avail', 0) < required:
            raise ValueError('insufficient target VM-pool headroom')
        name = f"lfs-{r['run']}-{r['stage']}"
        # Record intent before cloning, to make interrupted creation recoverable.
        active = {'run': r['run'], 'stage': r['stage'], 'name': name}
        self.state.write_text(json.dumps(active))
        command(['qm', 'clone', str(self.c['template']), self.vmid, '--full', '1',
                 '--storage', self.c['storage'], '--name', name,
                 '--description', f"lfs-proxmox:{r['run']}:{r['stage']}"], stdout=sys.stderr)
        command(['qm', 'set', self.vmid, '--cores', str(self.c['cores']),
                 '--memory', str(self.c['memory_mib']), '--balloon', '0', '--onboot', '0',
                 '--cpu', 'host', '--ciupgrade', '0', '--ciuser', 'ubuntu',
                 '--sshkeys', str(self.root / 'guest_key.pub'),
                 '--description', f"lfs-proxmox:{r['run']}:{r['stage']}",
                 '--net0', f"virtio,bridge={self.c['bridge']}",
                 '--ipconfig0', f"ip={self.c['address']}/24,gw={self.c['gateway']}",
                 '--nameserver', '1.1.1.1 9.9.9.9'], stdout=sys.stderr)
        command(['qm', 'resize', self.vmid, 'scsi0', f"{self.c['disk_gib']}G"], stdout=sys.stderr)
        drive = self.vmconfig()['scsi0']
        command(['qm', 'set', self.vmid, '--scsi0', drive + ',mbps=100,iops=20000',
                 '--cpulimit', str(self.c['cores'])], stdout=sys.stderr)
        self.guard(r)
        command(['qm', 'start', self.vmid], stdout=sys.stderr)

    def ready(self):
        check = ('import json, subprocess; '
                 'p = subprocess.run(["cloud-init", "status", "--format", "json"], '
                 'capture_output=True, text=True); s = json.loads(p.stdout); '
                 'assert s["status"] == "done" and not s.get("errors"), '
                 '"cloud-init is not successfully finished"')
        probe = ('set -e; python3 -c ' + shlex.quote(check) + '; '
                 'usermod -aG docker ubuntu; '
                 'install -d -o ubuntu -g ubuntu /home/ubuntu/lfs-cicd; '
                 'test -s /etc/ssh/ssh_host_ed25519_key.pub; '
                 'cat /etc/ssh/ssh_host_ed25519_key.pub')
        result = json.loads(output(['qm', 'guest', 'exec', self.vmid, '--timeout', '10',
                                     '--', '/bin/bash', '-c', probe]))
        if result.get('exitcode') != 0 or not result.get('exited'):
            raise ValueError('guest initialization not finished')
        pub = result['out-data'].strip().splitlines()[-1]
        if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: .*)?', pub):
            raise ValueError('unexpected guest host key')
        (self.root / 'known_hosts').write_text(f"{self.c['address']} {pub}\n")
        self.ssh('true', stdout=sys.stderr)
        print(json.dumps({'ready': True, 'vmid': self.vmid}))

    def execute(self, r):
        env = {'TERM': 'linux', **r.get('env', {})}
        exports = '\n'.join(f'export {k}={shlex.quote(v)}' for k, v in env.items())
        script = f"#!/bin/bash\ncd {WORK}\n{exports}\n{r['script']}\n"
        remote = (f"step_file=$(mktemp {WORK}/.lfs-step.XXXXXX); "
                  'cat > "$step_file"; '
                  f"cd {WORK}; timeout --kill-after=30 {r.get('timeout', 21600)} "
                  'script -q -e -c "stty cols 80 rows 24; bash -e -o pipefail \\\"$step_file\\\"" /dev/null; '
                  'result=$?; rm -f "$step_file"; exit "$result"')
        self.ssh(remote, input=script.encode())

    def verify(self):
        targets = [self.c['gateway'], *self.c['protected_addresses']]
        probe = '''import socket
targets = TARGETS
for target in targets:
    try:
        connection = socket.create_connection((target, 22), timeout=3)
    except OSError:
        continue
    connection.close()
    raise SystemExit("Isolation failure: a protected SSH endpoint was reachable")
print("Protected host endpoints are unreachable from the guest")
'''.replace('TARGETS', repr(targets))
        self.ssh('python3 -', input=probe.encode())
        self.ssh('curl -4 -fsSL --connect-timeout 10 --max-time 45 '
                 'https://www.linuxfromscratch.org/lfs/ >/dev/null')
        print('Isolation check passed; public HTTPS downloads work')

    def cache(self, r):
        cache = self.root / 'cache'
        cache.mkdir(mode=0o700, exist_ok=True)
        path = cache / r['key']
        if r['op'] == 'restore':
            if not path.is_file():
                raise ValueError(f"missing checkpoint {r['key']}; run the preceding stage first")
            with path.open('rb') as stream:
                self.ssh(f'tar -xf - -C {WORK}', stdin=stream)
            if r['path'] == 'jhalfs':
                # Preserve empty bind-mount scaffolding, including checkpoints
                # saved by the first adapter version that excluded this directory.
                self.ssh(f'mkdir -p {WORK}/jhalfs/mnt/build_dir')
            return
        if path.exists():
            raise ValueError('checkpoint already exists; use a new workflow run attempt')
        if shutil.disk_usage(cache).free < (self.c['min_free_gib'] + 32) * 2**30:
            raise ValueError('insufficient checkpoint storage headroom')
        temporary = path.with_suffix(path.suffix + '.partial')
        try:
            with temporary.open('xb') as stream:
                # Never extract a guest-controlled archive on Proxmox.
                # Keep mount-point directories, while omitting mounted contents.
                self.receive(f'tar --one-file-system -cf - '
                             f'-C {WORK} {shlex.quote(r["path"])}', stream)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def dispatch(self, r):
        if r['op'] == 'cleanup':
            if not self.state.exists():
                return
            active = json.loads(self.state.read_text())
            if active['run'] != r['run']:
                raise ValueError('refusing cleanup of another run')
            return self.dispatch({**r, 'op': 'destroy', 'stage': active['stage']})
        if r['op'] == 'health':
            self.network()
            print(json.dumps({'isolated': True, 'template': self.c['template'], 'vmid': self.vmid}))
            return
        if r['op'] == 'create':
            return self.create(r)
        self.guard(r)
        if r['op'] != 'destroy':
            self.network()
        if r['op'] == 'ready':
            return self.ready()
        if r['op'] == 'verify':
            return self.verify()
        if r['op'] == 'upload':
            return self.ssh(f'tar -xf - -C {WORK}')
        if r['op'] == 'exec':
            return self.execute(r)
        if r['op'] in {'save', 'restore'}:
            return self.cache(r)
        if r['op'] == 'artifact':
            # Fixed basename only. Symlinks can read guest files, never host files.
            maximum = (32 if r['name'] == 'build_dir.img' else 2) * 2**30
            return self.receive(f'cat -- {WORK}/{shlex.quote(r["name"])}',
                                sys.stdout.buffer, maximum=maximum)
        if r['op'] == 'destroy':
            command(['qm', 'stop', self.vmid], stdout=sys.stderr)
            command(['qm', 'destroy', self.vmid], stdout=sys.stderr)
            self.state.unlink()
            (self.root / 'known_hosts').unlink(missing_ok=True)


def fingerprint(raw):
    value = json.loads(raw)
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items() if k not in {'handle', 'metainfo'}}
        if isinstance(obj, list):
            return [clean(v) for v in obj if not (isinstance(v, dict) and 'metainfo' in v)]
        return obj
    return hashlib.sha256(json.dumps(clean(value), sort_keys=True).encode()).hexdigest()


def main():
    if os.geteuid() != 0:
        raise ValueError('broker must run as root through the restricted SSH command')
    r = request(sys.argv[1])
    with open('/run/lock/lfs-proxmox.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        broker = Broker(json.loads(CONFIG.read_text()))
        # Keep cancellation cleanup available while a guest command is running.
        # Lifecycle changes remain serialized and ownership-checked.
        if r['op'] not in {'create', 'destroy', 'cleanup'}:
            fcntl.flock(lock, fcntl.LOCK_UN)
        broker.dispatch(r)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'lfs-proxmox: {exc}', file=sys.stderr)
        sys.exit(1)
