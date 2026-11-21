import importlib.util
import io
import json
import shlex
import tarfile
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/proxmox' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


broker = load('broker', 'broker.py')
adapter = load('adapter', 'run.py')
installer = load('installer', 'install-host.py')


class RequestTests(unittest.TestCase):
    def req(self, **kwargs):
        return broker.request(json.dumps(dict(op='exec', run='123-1', stage='part2',
                                             script='true', **kwargs)))

    def test_rejects_unknown_host_operations(self):
        for op in ('shell', 'reboot-host', 'delete-vm', 'qm'):
            with self.subTest(op=op), self.assertRaises(ValueError):
                broker.request(json.dumps(dict(op=op)))

    def test_run_and_stage_cannot_inject_commands(self):
        for field, bad in [('run', '1;reboot'), ('run', '../123-1'), ('stage', '9000')]:
            req = dict(op='destroy', run='123-1', stage='part2')
            req[field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                broker.request(json.dumps(req))

    def test_cache_and_artifact_paths_cannot_escape(self):
        for op, extra in [('save', dict(key='../etc/passwd', path='jhalfs')),
                          ('restore', dict(key='valid', path='/etc/passwd')),
                          ('artifact', dict(name='../private-key')),
                          ('artifact', dict(name='..'))]:
            with self.subTest(op=op, extra=extra), self.assertRaises(ValueError):
                broker.request(json.dumps(dict(op=op, run='123-1', stage='part2', **extra)))

    def test_guest_environment_must_have_valid_names(self):
        with self.assertRaises(ValueError):
            self.req(env={'PATH;reboot': 'x'})
        self.req(env={'TARGET': "x'; reboot #"})  # quoted only inside the guest

    def test_caller_cannot_override_control_plane_resources(self):
        for field in ('vmid', 'template', 'bridge', 'state_dir'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                broker.request(json.dumps(dict(op='create', run='123-1', stage='part2',
                                               **{field: 'unrelated-resource'})))


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b = object.__new__(broker.Broker)
        self.b.root = Path(self.temp.name)
        self.b.state = self.b.root / 'active.json'
        self.b.vmid = '9100'
        self.b.c = dict(bridge='vmbr99')
        self.b.state.write_text(json.dumps(dict(run='123-1', stage='part2', name='lfs-test')))
        self.b.vmconfig = MagicMock(return_value=dict(
            name='lfs-test', description='lfs-proxmox:123-1:part2',
            scsi0='local-lvm:vm-9100-disk-0,size=100G',
            ide2='local-lvm:vm-9100-cloudinit,media=cdrom',
            net0='virtio=02:00:00:00:00:01,bridge=vmbr99'))
        self.r = dict(op='destroy', run='123-1', stage='part2')

    def test_only_matching_run_may_destroy(self):
        self.b.guard(self.r)
        with self.assertRaises(ValueError):
            self.b.guard({**self.r, 'run': '124-1'})

    def test_name_marker_template_and_network_changes_refused(self):
        original = self.b.vmconfig.return_value
        for field, bad in [('name', 'important-vm'), ('description', 'other-owner'),
                           ('template', 1), ('net0', 'virtio,bridge=vmbr99evil'),
                           ('net1', 'virtio,bridge=vmbr0'), ('hostpci0', '00:00.0'),
                           ('scsi1', '/dev/sda'), ('scsi0', 'local-lvm:vm-201-disk-0'),
                           ('hookscript', 'local:snippets/host-code.pl'), ('usb0', 'host=1-1')]:
            self.b.vmconfig.return_value = {**original, field: bad}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.b.guard(self.r)

    def test_cleanup_of_other_run_never_stops_any_vm(self):
        with patch.object(broker, 'command') as command, self.assertRaises(ValueError):
            self.b.dispatch({**self.r, 'op': 'cleanup', 'run': '124-1'})
        command.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_current_upstream_workflow_is_supported(self):
        import yaml
        source = ROOT / '.github/workflows/linux-gha.yaml'
        workflow = yaml.safe_load(source.read_text())
        adapter.validate_workflow(workflow)
        for job in workflow['jobs'].values():
            for step in job['steps']:
                if step.get('name') not in adapter.PLATFORM_STEPS:
                    if 'run' in step:
                        adapter.expand(step['run'], 'a' * 40)

    def test_unknown_conditions_and_expressions_fail_closed(self):
        with self.assertRaises(ValueError):
            adapter.condition('success() || untrusted', False)
        with self.assertRaises(ValueError):
            adapter.expand('${{ secrets.SOMETHING }}', 'a' * 40)
        self.assertFalse(adapter.condition(None, True))
        self.assertTrue(adapter.condition('always()', True))
        self.assertFalse(adapter.condition('${{ false }}', False))

    def test_failed_build_runs_diagnostics_and_cleanup(self):
        client = MagicMock()
        client.run = '123-1'
        def call(op, *args, **kwargs):
            if op == 'exec' and kwargs.get('script') == 'fail':
                raise subprocess.CalledProcessError(1, 'guest-build')
        client.call.side_effect = call
        job = dict(steps=[dict(name='build', run='fail'),
                          dict(name='skip', run='must-not-run'),
                          dict(name='diagnostics', run='diagnose', **{'if': 'always()'})])
        with patch.object(adapter, 'upload'), tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError):
                adapter.run_stage(client, 'part2', job, 'a' * 40, Path(temp))
        scripts = [c.kwargs.get('script') for c in client.call.call_args_list]
        self.assertIn('diagnose', scripts)
        self.assertNotIn('must-not-run', scripts)
        self.assertEqual(client.call.call_args_list[-1].args, ('destroy', 'part2'))

    def test_cache_saves_happen_after_build_not_at_cache_step(self):
        client = MagicMock()
        client.run = '123-1'
        job = dict(steps=[{'name': 'write cache jhalfs', 'uses': 'actions/cache@v6',
                           'with': {'path': 'jhalfs', 'key': '${{ runner.os }}-key'}},
                          {'name': 'build', 'run': 'build'}])
        with patch.object(adapter, 'upload'), tempfile.TemporaryDirectory() as temp:
            adapter.run_stage(client, 'part2', job, 'a' * 40, Path(temp))
        operations = [c.args[0] for c in client.call.call_args_list]
        self.assertLess(operations.index('exec'), operations.index('save'))

    def test_guest_output_size_is_bounded(self):
        b = object.__new__(broker.Broker)
        b.ssh_args = lambda _: ['ssh', 'guest']
        proc = MagicMock()
        proc.stdout = io.BytesIO(b'oversized')
        with patch.object(broker.subprocess, 'Popen', return_value=proc):
            with self.assertRaises(ValueError):
                b.receive('cat', io.BytesIO(), maximum=2)

    def test_low_controller_space_prevents_artifact_transfer(self):
        client = MagicMock()
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(adapter.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
                with self.assertRaises(ValueError):
                    adapter.artifact(client, 'part4', 'build_dir.img', Path(temp))
        client.call.assert_not_called()

    def test_old_metadata_checkpoint_restores_mount_scaffolding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'cache').mkdir()
            work = root / 'work'
            work.mkdir()
            with tarfile.open(root / 'cache/checkpoint', 'w') as archive:
                item = tarfile.TarInfo('jhalfs')
                item.type = tarfile.DIRTYPE
                item.mode = 0o755
                archive.addfile(item)
            b = object.__new__(broker.Broker)
            b.root = root
            b.ssh = lambda remote, **kwargs: subprocess.run(shlex.split(remote), check=True, **kwargs)
            with patch.object(broker, 'WORK', str(work)):
                b.cache(dict(op='restore', key='checkpoint', path='jhalfs'))
            self.assertTrue((work / 'jhalfs/mnt/build_dir').is_dir())

    def test_installer_rejects_interface_injection_before_mutations(self):
        argv = ['install-host.py', '--uplink', 'vmbr0;reboot', '--storage', 'pool',
                '--space-path', '/data', '--state-dir', '/data/lfs',
                '--runner-public-key', '/tmp/key.pub']
        with patch.object(sys, 'argv', argv), patch.object(installer.os, 'geteuid', return_value=0):
            with patch.object(installer, 'run') as run, self.assertRaises(SystemExit):
                installer.main()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
