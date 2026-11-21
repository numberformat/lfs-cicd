#!/usr/bin/env python3
"""Mac/local-runner adapter for the unchanged upstream build stages."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[2]
STAGES = ('part2', 'part3luser', 'part3shadow', 'part3gcc', 'part3remaining', 'part4')
WORK = '/home/ubuntu/lfs-cicd'
# These steps manage the hosted runner or publish repository changes, not LFS.
# The source workflow is never edited. Publishing needs separate authorization.
PLATFORM_STEPS = {'increase runner disk space', 'git config', 'commit sbu report'}


def expand(value, sha):
    replacements = {'github.workspace': WORK, 'github.sha': sha, 'runner.os': 'Linux'}
    def replace(match):
        key = match.group(1).strip()
        if key not in replacements:
            raise ValueError(f'unsupported workflow expression: {key}')
        return replacements[key]
    return re.sub(r'\$\{\{\s*(.*?)\s*\}\}', replace, str(value))


def condition(value, failed):
    if value is None:
        return not failed
    text = str(value).strip().removeprefix('${{').removesuffix('}}').strip()
    if text in {'false', 'False'}:
        return False
    if text == 'always()':
        return True
    raise ValueError(f'unsupported condition: {value}')


class Client:
    def __init__(self, host, key, run):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
            raise ValueError('host must be a hostname or IPv4 address, without a username')
        if not re.fullmatch(r'[0-9]{1,20}-[0-9]{1,5}', run):
            raise ValueError('invalid run ID-attempt')
        self.base = ['ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                     '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                     '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                     '-o', 'IdentityAgent=none',
                     '-i', str(Path(key).expanduser().resolve()), f'root@{host}']
        self.run = run

    def call(self, op, stage=None, **kwargs):
        io = {k: kwargs.pop(k) for k in ('stdin', 'stdout', 'stderr', 'input') if k in kwargs}
        req = {'op': op, 'run': self.run, 'stage': stage, **kwargs}
        # The forced command parses SSH_ORIGINAL_COMMAND as JSON, not shell code.
        # Send raw JSON; additional shell quoting would change the protocol.
        return subprocess.run(self.base + [json.dumps(req)], check=True, **io)


def upload(client, stage):
    # Only committed files; no .git, checkout token, local keys, or runner state.
    archive = subprocess.Popen(['git', 'archive', '--format=tar', 'HEAD'],
                               cwd=ROOT, stdout=subprocess.PIPE)
    try:
        client.call('upload', stage, stdin=archive.stdout)
    finally:
        archive.stdout.close()
        if archive.wait() != 0:
            raise RuntimeError('source archive failed')


def artifact(client, stage, name, destination):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in {'.', '..'}:
        raise ValueError('invalid artifact basename')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    maximum_gib = 32 if name == 'build_dir.img' else 2
    if shutil.disk_usage(destination).free < (maximum_gib + 10) * 2**30:
        raise ValueError('insufficient local artifact space with a 10 GiB reserve')
    target = destination / name
    temporary = destination / (name + '.partial')
    try:
        with temporary.open('xb') as stream:
            client.call('artifact', stage, name=name, stdout=stream)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def validate_workflow(workflow):
    jobs = workflow['jobs']
    if tuple(jobs) != STAGES:
        raise ValueError('upstream stages changed; review the adapter before building')
    for stage, job in jobs.items():
        expected = None if stage == STAGES[0] else STAGES[STAGES.index(stage) - 1]
        if job.get('needs') != expected:
            raise ValueError(f'upstream dependencies changed in {stage}')
        for step in job['steps']:
            condition(step.get('if'), False)
            if 'uses' in step and not step['uses'].startswith(
                ('actions/checkout@', 'actions/cache@', 'actions/upload-artifact@')
            ):
                raise ValueError(f'unsupported upstream action: {step["uses"]}')
            if 'shell' in step and step['shell'] != 'script -q -e -c "bash {0}"':
                raise ValueError(f'upstream shell changed: {step["shell"]}')


def run_stage(client, stage, job, sha, destination, smoke=False):
    failed = False
    created = False
    deferred = []
    try:
        # A failed create may have left an ownership-marked VM; cleanup still runs.
        created = True
        client.call('create', stage)
        for attempt in range(60):
            try:
                client.call('ready', stage, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except subprocess.CalledProcessError:
                if attempt == 59:
                    raise RuntimeError('guest not ready after ten minutes')
                time.sleep(10)
        client.call('verify', stage)
        if smoke:
            client.call('exec', stage, script='uname -r; cloud-init status --long || test "$?" -eq 2; '
                        'docker --version; docker compose version; '
                        'qemu-system-x86_64 --version; test -s /etc/machine-id', timeout=60)
            return
        upload(client, stage)
        for step in job['steps']:
            name = step.get('name', step.get('uses', 'unnamed'))
            if not condition(step.get('if'), failed):
                continue
            print(f'[{stage}] {name}', flush=True)
            if name in PLATFORM_STEPS:
                print('  Deployment-only step skipped; upstream source is unchanged.', flush=True)
                continue
            try:
                if 'run' in step:
                    script = expand(step['run'], sha)
                    env = {k: expand(v, sha) for k, v in step.get('env', {}).items()}
                    client.call('exec', stage, script=script, env=env,
                                timeout=int(step.get('timeout-minutes', 360)) * 60)
                elif step['uses'].startswith('actions/checkout@'):
                    pass  # git archive uploaded the exact committed checkout.
                elif step['uses'].startswith('actions/cache@'):
                    opts = step['with']
                    key = f'{client.run}-' + expand(opts['key'], sha)
                    paths = opts['path'].strip().splitlines()
                    if len(paths) != 1:
                        raise ValueError('adapter expects one path per upstream cache step')
                    path = paths[0].strip()
                    if name.startswith('read cache'):
                        client.call('restore', stage, key=key, path=path)
                    elif name.startswith('write cache'):
                        deferred.append((key, path))
                    else:
                        raise ValueError('unknown upstream cache operation')
                elif step['uses'].startswith('actions/upload-artifact@'):
                    name = Path(expand(step['with']['path'], sha)).name
                    if name == '*SBU_DU*.report':
                        client.call('exec', stage, script='cp -- *SBU_DU*.report sbu.report', timeout=60)
                        name = 'sbu.report'
                    artifact(client, stage, name, destination / stage)
            except (subprocess.CalledProcessError, ValueError, OSError) as exc:
                failed = True
                print(f'[{stage}] step failed: {exc}', file=sys.stderr, flush=True)
        if failed:
            raise RuntimeError(f'{stage} failed; completed checkpoints remain available')
        # actions/cache saves at job completion, not at the point it appears.
        for key, path in deferred:
            client.call('save', stage, key=key, path=path)
        if stage == 'part4':
            artifact(client, stage, 'build_dir.img', destination)
    finally:
        if created:
            try:
                client.call('destroy', stage)
            except subprocess.CalledProcessError:
                print('CLEANUP FAILED: investigate the designated builder slot; '
                      'no other VM will be deleted.', file=sys.stderr)
                raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=('plan', 'smoke', 'build', 'cleanup'), default='plan')
    p.add_argument('--stage', choices=STAGES)
    p.add_argument('--run', default=f'{os.getenv("GITHUB_RUN_ID", str(int(time.time())))}-'
                   f'{os.getenv("GITHUB_RUN_ATTEMPT", "1")}')
    args = p.parse_args()
    if args.mode == 'cleanup':
        client = Client(os.environ['LFS_PROXMOX_HOST'], os.environ['LFS_PROXMOX_KEY_PATH'], args.run)
        client.call('cleanup', STAGES[0])
        return
    workflow_path = ROOT / '.github/workflows/linux-gha.yaml'
    workflow = yaml.safe_load(workflow_path.read_text())
    validate_workflow(workflow)
    stages = (args.stage,) if args.stage else STAGES
    if args.mode == 'plan':
        for stage in stages:
            print(f'{stage}: {len(workflow["jobs"][stage]["steps"])} upstream steps; '
                  'fresh VM, isolation check, existing checkpoint boundaries, cleanup')
        return
    # Refuse to silently build a different checkout from the one being reviewed.
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True)
    if args.mode == 'build' and dirty:
        p.error('commit the reviewed adapter and checkout before building; local secrets must be ignored')
    client = Client(os.environ['LFS_PROXMOX_HOST'], os.environ['LFS_PROXMOX_KEY_PATH'], args.run)
    client.call('health')
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    destination = (Path(os.getenv('LFS_PROXMOX_ARTIFACTS') or
        str(Path.home() / '.local/share/lfs-proxmox/artifacts')).expanduser().resolve()
        / args.run)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    (destination / 'manifest.json').write_text(json.dumps(dict(
        repository_commit=sha, run=args.run, stages=stages,
        upstream_workflow=str(workflow_path.relative_to(ROOT))), indent=2) + '\n')
    for stage in stages:
        run_stage(client, stage, workflow['jobs'][stage], sha, destination,
                  smoke=args.mode == 'smoke')
        if args.mode == 'smoke':
            break
    print(f'Artifacts: {destination}', flush=True)


if __name__ == '__main__':
    def cancel(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, cancel)
    main()
