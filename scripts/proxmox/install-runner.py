#!/usr/bin/env python3
"""Install a separate repository-scoped macOS GitHub Actions runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess


def gh(*args):
    # Registration tokens stay in memory and are never printed or saved.
    return json.loads(subprocess.check_output(['gh', 'api', *args], text=True))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', required=True, help='OWNER/REPOSITORY')
    p.add_argument('--name', default='macbook-lfs-proxmox')
    p.add_argument('--directory', type=Path,
                   default=Path.home() / '.local/share/lfs-proxmox/runner')
    p.add_argument('--proxmox-host', help='also verify connectivity and set repository variables')
    p.add_argument('--key-path', type=Path,
                   default=Path.home() / '.config/lfs-proxmox/runner_key')
    p.add_argument('--artifacts-path', type=Path,
                   default=Path.home() / '.local/share/lfs-proxmox/artifacts')
    args = p.parse_args()
    if platform.system() != 'Darwin' or os.geteuid() == 0:
        p.error('run on macOS as the logged-in runner user, without sudo')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        p.error('repo must be OWNER/REPOSITORY')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.name):
        p.error('invalid runner name')
    directory = args.directory.expanduser().resolve()
    key = args.key_path.expanduser().resolve()
    artifacts = args.artifacts_path.expanduser().resolve()
    env = {k: os.environ[k] for k in ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'SHELL')
           if k in os.environ}
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    if args.proxmox_host:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', args.proxmox_host):
            p.error('invalid Proxmox hostname')
        if not key.is_file():
            p.error('restricted Proxmox key does not exist')
        subprocess.run(['ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes',
                        '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                        '-i', str(key), f'root@{args.proxmox_host}', '{"op":"health"}'], check=True)
    repo_url = f'https://github.com/{args.repo}'
    settings = directory / '.runner'
    if settings.exists():
        current = json.loads(settings.read_text(encoding='utf-8-sig'))
        if current.get('gitHubUrl', '').rstrip('/') != repo_url or current.get('agentName') != args.name:
            p.error('directory belongs to a different runner; refusing to replace it')
        print('Using the existing matching repository runner.', flush=True)
    else:
        if directory.exists() and any(directory.iterdir()):
            p.error('directory is nonempty and not registered; inspect it before retrying')
        arch = {'arm64': 'arm64', 'x86_64': 'x64'}.get(platform.machine())
        if not arch:
            p.error('unsupported Mac architecture')
        runners = gh(f'repos/{args.repo}/actions/runners')['runners']
        if any(r['name'] == args.name for r in runners):
            p.error('runner name already exists on GitHub; no replacement will be made')
        release = gh('repos/actions/runner/releases/latest')
        version = release['tag_name'].removeprefix('v')
        filename = f'actions-runner-osx-{arch}-{version}.tar.gz'
        asset = next(a for a in release['assets'] if a['name'] == filename)
        digest = asset.get('digest', '')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            p.error('official release has no usable SHA-256 digest')
        url = asset['browser_download_url']
        if not url.startswith('https://github.com/actions/runner/releases/download/'):
            p.error('unexpected release download origin')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(directory).free < 2 * 2**30:
            p.error('at least 2 GiB free space is needed for installation')
        archive = directory / filename
        print(f'Downloading official runner {version} ({arch})', flush=True)
        subprocess.run(['curl', '-fL', '--retry', '3', '--output', str(archive), url], check=True)
        calculated = hashlib.sha256()
        with archive.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                calculated.update(chunk)
        if calculated.hexdigest() != digest.split(':', 1)[1]:
            raise RuntimeError('runner archive checksum mismatch; refusing extraction')
        subprocess.run(['tar', '-xzf', str(archive), '-C', str(directory)], check=True)
        archive.unlink()
        token = gh('--method', 'POST', f'repos/{args.repo}/actions/runners/registration-token')['token']
        # Do not use check=True here: CalledProcessError would include the token
        # in its command representation if configuration failed.
        result = subprocess.run(['./config.sh', '--unattended', '--url', repo_url,
                                 '--token', token, '--name', args.name,
                                 '--labels', 'lfs-proxmox', '--work', '_work'], cwd=directory, env=env)
        del token
        if result.returncode:
            raise RuntimeError('runner registration failed; inspect its diagnostics before retrying')
    # svc.sh creates a per-user LaunchAgent, separate from organization runners.
    if not (directory / '.service').exists():
        subprocess.run(['./svc.sh', 'install'], cwd=directory, env=env, check=True)
    plist = Path((directory / '.service').read_text().strip())
    with plist.open('rb') as stream:
        label = plistlib.load(stream)['Label']
    running = subprocess.run(['launchctl', 'list', label], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL).returncode == 0
    subprocess.run(['./svc.sh', 'status' if running else 'start'],
                   cwd=directory, env=env, check=True)
    if args.proxmox_host:
        variables = dict(LFS_PROXMOX_HOST=args.proxmox_host,
                         LFS_PROXMOX_KEY_PATH=str(key), LFS_PROXMOX_ARTIFACTS=str(artifacts))
        for name, value in variables.items():
            subprocess.run(['gh', 'variable', 'set', name, '--repo', args.repo,
                            '--body', value], check=True)
        print('Configured the three Proxmox Actions variables (no private key uploaded).', flush=True)
    print(f'Runner installed: {directory}\nRepository: {repo_url}\nLabel: lfs-proxmox', flush=True)


if __name__ == '__main__':
    main()
