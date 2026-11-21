#!/usr/bin/env python3
"""One-time administrator setup, run on Proxmox, never by a build job."""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess


def run(*args, **kwargs):
    kwargs.setdefault('check', True)
    return subprocess.run(args, **kwargs)


def write(path, text, mode=0o600):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    target.chmod(mode)


def firewall(bridge, uplink, subnet, address):
    denied = ('0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, '
              '169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24, '
              '192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, '
              '203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4')
    return f'''table inet lfs_proxmox {{
  set denied4 {{ type ipv4_addr; flags interval; elements = {{ {denied} }}; }}
  chain input {{
    type filter hook input priority -20; policy accept;
    iifname "{bridge}" meta nfproto ipv6 drop
    iifname "{bridge}" ip saddr != {address} drop
    iifname "{bridge}" ct state established,related accept
    iifname "{bridge}" drop
  }}
  chain forward {{
    type filter hook forward priority -20; policy accept;
    iifname "{bridge}" meta nfproto ipv6 drop
    oifname "{bridge}" meta nfproto ipv6 drop
    iifname "{bridge}" ip saddr != {address} drop
    iifname "{bridge}" ip daddr @denied4 drop
    iifname "{bridge}" oifname "{uplink}" accept
    iifname "{bridge}" drop
    oifname "{bridge}" ct state established,related accept
    oifname "{bridge}" drop
  }}
  chain nat {{
    type nat hook postrouting priority srcnat; policy accept;
    iifname "{bridge}" ip saddr {subnet} oifname "{uplink}" masquerade
  }}
}}
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bridge', default='vmbr99')
    p.add_argument('--uplink', required=True)
    p.add_argument('--subnet', default='172.30.240.0/24')
    p.add_argument('--template', type=int, default=9001)
    p.add_argument('--vmid', type=int, default=9100)
    p.add_argument('--cores', type=int, default=4)
    p.add_argument('--storage', required=True)
    p.add_argument('--space-path', required=True)
    p.add_argument('--state-dir', required=True)
    p.add_argument('--runner-public-key', required=True)
    args = p.parse_args()
    if os.geteuid():
        p.error('run as root on Proxmox')
    for name in (args.bridge, args.uplink):
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,14}', name):
            p.error('invalid interface name')
    if args.bridge == args.uplink or args.vmid == args.template or args.vmid < 9000:
        p.error('use a separate bridge and a separate builder VM ID >=9000')
    if not 1 <= args.cores <= 256:
        p.error('cores must be between 1 and 256')
    subnet = ipaddress.IPv4Network(args.subnet)
    if subnet.prefixlen != 24 or not subnet.is_private:
        p.error('use an unused private IPv4 /24')
    routes = json.loads(run('ip', '-j', '-4', 'route', stdout=subprocess.PIPE, text=True).stdout)
    for route in routes:
        dest = route.get('dst', 'default')
        if dest != 'default' and subnet.overlaps(ipaddress.IPv4Network(dest, strict=False)):
            p.error(f'subnet overlaps existing route: {dest}')
    if Path(f'/sys/class/net/{args.bridge}').exists():
        p.error('bridge already exists; inspect it rather than overwriting its configuration')
    if Path(f'/etc/pve/qemu-server/{args.vmid}.conf').exists():
        p.error('builder VM ID is occupied')
    if not Path(f'/sys/class/net/{args.uplink}').exists():
        p.error('uplink does not exist')
    # Never enable routing globally or change an existing interface silently.
    if Path(f'/proc/sys/net/ipv4/conf/{args.uplink}/forwarding').read_text().strip() != '1':
        p.error('uplink must already support IPv4 forwarding; review host routing first')
    pub = Path(args.runner_public_key).read_text().strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', pub):
        p.error('expected one Ed25519 runner public key')
    if Path('/etc/lfs-proxmox/config.json').exists():
        p.error('already installed; review existing configuration before updating')
    if run('nft', 'list', 'table', 'inet', 'lfs_proxmox',
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0:
        p.error('nft table name is already in use')
    gateway, address = str(subnet[1]), str(subnet[10])
    uplink_addresses = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev',
                                     args.uplink, stdout=subprocess.PIPE, text=True).stdout)
    protected = [a['local'] for a in uplink_addresses[0]['addr_info'] if a['family'] == 'inet']
    cfg = dict(bridge=args.bridge, uplink=args.uplink, gateway=gateway, address=address,
               template=args.template, vmid=args.vmid, storage=args.storage,
               state_dir=args.state_dir, space_path=args.space_path,
               cores=args.cores, memory_mib=16384, disk_gib=100, min_free_gib=100,
               protected_addresses=protected)
    rules = firewall(args.bridge, args.uplink, subnet, address)
    Path('/etc/lfs-proxmox').mkdir(mode=0o700)
    write('/etc/lfs-proxmox/rules.nft', rules)
    run('nft', '--check', '-f', '/etc/lfs-proxmox/rules.nft')
    write('/etc/network/interfaces.d/lfs-proxmox', f'''# Managed by lfs-proxmox; no physical uplink.
auto {args.bridge}
iface {args.bridge} inet static
    address {gateway}/24
    bridge-ports none
    bridge-stp off
    bridge-fd 0
    post-up sysctl -q -w net.ipv6.conf.{args.bridge}.disable_ipv6=1
''', 0o644)
    write('/usr/local/sbin/lfs-proxmox-network', f'''#!/bin/sh
set -eu
# Fail closed: no build NIC exists until the broker verifies this ruleset.
test -d /sys/class/net/{args.bridge} || ifup {args.bridge}
rules_file=$(mktemp)
trap 'rm -f "$rules_file"' EXIT
if nft list table inet lfs_proxmox >/dev/null 2>&1; then
  echo 'delete table inet lfs_proxmox' > "$rules_file"
fi
cat /etc/lfs-proxmox/rules.nft >> "$rules_file"
nft -f "$rules_file"
sysctl -q -w net.ipv6.conf.{args.bridge}.disable_ipv6=1
sysctl -q -w net.ipv4.conf.{args.bridge}.forwarding=1
sysctl -q -w net.ipv4.conf.{args.bridge}.send_redirects=0
sysctl -q -w net.ipv4.conf.{args.bridge}.accept_redirects=0
''', 0o755)
    write('/etc/systemd/system/lfs-proxmox-network.service', '''[Unit]
Description=Isolated LFS build network (only its own bridge and nft table)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/lfs-proxmox-network

[Install]
WantedBy=multi-user.target
''', 0o644)
    root = Path(args.state_dir)
    root.mkdir(mode=0o700, parents=True)
    write('/etc/lfs-proxmox/config.json', json.dumps(cfg, indent=2) + '\n')
    broker_path = Path(__file__).with_name('broker.py')
    shutil.copyfile(broker_path, '/usr/local/sbin/lfs-proxmox-broker')
    Path('/usr/local/sbin/lfs-proxmox-broker').chmod(0o755)
    spec = importlib.util.spec_from_file_location('broker', broker_path)
    broker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broker)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'lfs-proxmox-network.service')
    nft = run('nft', '-j', 'list', 'table', 'inet', 'lfs_proxmox',
              stdout=subprocess.PIPE, text=True).stdout
    write('/etc/lfs-proxmox/nft.sha256', broker.fingerprint(nft) + '\n')
    run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / 'guest_key'))
    # A dedicated forced-command key avoids requiring sudo on minimal Proxmox.
    # Existing administrator keys and sshd settings are not changed.
    home = Path('/root/.ssh')
    home.mkdir(mode=0o700, exist_ok=True)
    authorized = home / 'authorized_keys'
    previous = authorized.read_text() if authorized.exists() else ''
    write(authorized, previous.rstrip('\n') + '\n' +
          'restrict,command="/usr/local/libexec/lfs-proxmox-ssh" ' + pub + '\n')
    write('/usr/local/libexec/lfs-proxmox-ssh', '''#!/bin/sh
exec /usr/local/sbin/lfs-proxmox-broker "$SSH_ORIGINAL_COMMAND"
''', 0o755)
    broker.Broker(cfg).dispatch({'op': 'health'})


if __name__ == '__main__':
    main()
