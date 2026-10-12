"""Isolated shell integration checks; never mount filesystems or call real Asterisk."""
import json
import concurrent.futures
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import signal
import time
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1] / 'node-speak'


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.ram = self.root / 'ram'
        self.ram.mkdir()
        voice = self.root / 'voice.onnx'
        self.voice = voice
        voice.touch()
        Path(str(voice) + '.json').write_text(json.dumps({'audio': {'sample_rate': 16000}}))
        self.fstab = self.root / 'fstab'
        self.fstab.write_text(f'# preserved\ntmpfs {self.ram} tmpfs defaults 0 0\n')
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.unit = self.root / 'node-speak.service'
        self.installed = self.root / 'installed'
        self.script = self.root / 'script'
        self.script.write_text(SOURCE.read_text()
            .replace('/var/lib/piper-tts/en_US-amy-low.onnx', str(voice))
            .replace('/var/lib/asterisk/sounds/ramdisk', str(self.ram))
            .replace('/etc/fstab', str(self.fstab))
            .replace('/usr/local/bin/node-speak', str(self.installed))
            .replace('/etc/systemd/system/node-speak.service', str(self.unit))
            .replace('/run/node-speak', str(self.runtime))
            .replace('[[ $EUID -eq 0 ]]', '[[ 0 -eq 0 ]]'))
        mocks = {
            'findmnt': '[[ "${MOUNTED:-1}" == 1 ]] || exit 1\nprintf "%s\\n" "${FSTYPE:-tmpfs}"',
            'piper': 'if [[ "${GATE:-0}" == 1 ]]; then while [[ ! -e "$TEST_ROOT/release" ]]; do /bin/sleep 0.02; done; fi\ncat > "$TEST_ROOT/input"\n[[ "${EMPTY:-0}" == 1 ]] || printf audio\ncat "$TEST_ROOT/input" >> "$TEST_ROOT/order"\nexit "${PIPER_EXIT:-0}"',
            'sox': 'printf "%s\\n" "$*" > "$TEST_ROOT/sox_args"\ncat > "${@: -1}"\nexit "${SOX_EXIT:-0}"',
            'asterisk': 'case "$*" in *localplay*) printf "%s\\n" "$*" > "$TEST_ROOT/playback"; printf "%s\\n" "${PLAY_MESSAGE:-OK}"; exit "${PLAY_EXIT:-0}";; esac',
            'sleep': 'printf "%s\\n" "$*" > "$TEST_ROOT/delay"',
            'mount': 'exit "${MOUNT_EXIT:-0}"',
            'id': '[[ "${NO_ASTERISK:-0}" == 0 ]] || exit 1\ncase "$*" in -u*) echo 123;; -g*) echo 456;; esac',
            'chown': 'exit 0',
            'install': 'cp "${@: -2:1}" "${@: -1}"',
            'systemctl': 'printf "%s\\n" "$*" >> "$TEST_ROOT/systemctl"; exit "${SYSTEMCTL_EXIT:-0}"',
            'dpkg-query': 'if [[ "${PACKAGE_MISSING:-0}" == 1 && ! -e "$TEST_ROOT/package_installed" ]]; then exit 1; fi\nprintf "install ok installed"',
            'apt-get': 'printf "%s\\n" "$*" >> "$TEST_ROOT/apt_calls"\ncase "$1" in update) exit "${UPDATE_EXIT:-0}";; install) [[ "${INSTALL_EXIT:-0}" == 0 ]] || exit "$INSTALL_EXIT"; touch "$TEST_ROOT/package_installed";; esac',
        }
        for name, body in mocks.items():
            path = self.bin / name
            path.write_text('#!/bin/bash\n' + body + '\n')
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], TEST_ROOT=str(self.root))
        self.workers = []
        self.addCleanup(self.stop_workers)

    def run_script(self, args=None, **env):
        return subprocess.run(['/bin/bash', str(self.script), *(args or ['--play', '123', 'hello'])],
                              env=dict(self.env, **env), capture_output=True, text=True)

    def assert_clean(self):
        self.assertEqual(list(self.ram.glob('tts_*')), [])

    def test_success_and_model_rate(self):
        result = self.run_script(['--play', '123', '-n'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'input').read_text(), '-n\n')
        self.assertIn('-r 16000', (self.root / 'sox_args').read_text())
        self.assertEqual((self.root / 'delay').read_text(), '31\n')
        self.assert_clean()

    def test_render_failures(self):
        for env in ({'PIPER_EXIT': '7'}, {'SOX_EXIT': '8'}, {'EMPTY': '1'}):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_script(**env).returncode, 0)
                self.assertFalse((self.root / 'playback').exists())
                self.assert_clean()

    def test_playback_failure(self):
        self.assertNotEqual(self.run_script(PLAY_EXIT='9').returncode, 0)
        self.assert_clean()

    def test_cli_rejection(self):
        self.assertNotEqual(self.run_script(PLAY_MESSAGE='Node 123 not found').returncode, 0)
        self.assert_clean()

    def test_wrong_filesystem(self):
        self.assertNotEqual(self.run_script(FSTYPE='ext4').returncode, 0)
        self.assertFalse((self.root / 'playback').exists())

    def test_unmounted(self):
        self.assertNotEqual(self.run_script(MOUNTED='0').returncode, 0)

    def test_bad_arguments(self):
        for args in (['abc', 'hello'], ['123'], ['123', 'hello', 'extra']):
            self.assertNotEqual(self.run_script(args).returncode, 0)

    def test_setup_updates_fstab_and_is_idempotent(self):
        result = self.run_script(['--setup'])
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.fstab.read_text()
        self.assertIn('uid=123,gid=456', content)
        self.assertIn('User=asterisk', self.unit.read_text())
        self.assertIn('Group=456', self.unit.read_text())
        self.assertIn(f'ExecStart="{self.installed}" --worker', self.unit.read_text())
        self.assertIn('restart node-speak.service', (self.root / 'systemctl').read_text())
        self.assertTrue(content.startswith('# preserved\n'))
        self.assertEqual(content.count(str(self.ram)), 1)
        self.assertTrue(Path(str(self.fstab) + '.node-speak.bak').exists())
        self.assertEqual(self.run_script(['--setup']).returncode, 0)
        self.assertEqual(self.fstab.read_text(), content)

    def test_setup_mount_failure(self):
        before = self.fstab.read_text()
        result = self.run_script(['--setup'], MOUNTED='0', MOUNT_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Setup complete', result.stdout)
        self.assertEqual(self.fstab.read_text(), before)

    def stop_workers(self):
        (self.root / 'release').touch()
        deadline = time.monotonic() + 3
        while list(self.runtime.glob('job-*.running')) and time.monotonic() < deadline:
            time.sleep(0.02)
        for process in self.workers:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)

    def start_worker(self, **env):
        log = (self.root / 'worker.log').open('a')
        process = subprocess.Popen(['/bin/bash', str(self.script), '--worker'],
                                   env=dict(self.env, **env), stdout=log, stderr=log,
                                   start_new_session=True)
        log.close()
        self.workers.append(process)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            path = self.runtime / 'worker.lock'
            if path.exists():
                with path.open('r+') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return process
            if process.poll() is not None:
                self.fail((self.root / 'worker.log').read_text())
            time.sleep(0.02)
        self.fail('Worker did not become ready')

    def wait_for_order(self, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            path = self.root / 'order'
            if path.exists() and path.read_text().splitlines() == expected:
                return
            time.sleep(0.02)
        self.fail((self.root / 'worker.log').read_text())

    def test_submit_returns_before_rendering_and_preserves_order(self):
        self.start_worker(GATE='1')
        for node, text in [('123', 'first'), ('456', 'second'), ('123', 'third')]:
            started = time.monotonic()
            result = self.run_script([node, text])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Queued job-', result.stdout)
            self.assertLess(time.monotonic() - started, 1)
        self.assertFalse((self.root / 'order').exists())
        (self.root / 'release').touch()
        self.wait_for_order(['first', 'second', 'third'])

    def test_concurrent_submission_admission_order_matches_playback(self):
        self.start_worker(GATE='1')
        def submit(index):
            result = self.run_script(['123', f'text-{index}'])
            self.assertEqual(result.returncode, 0, result.stderr)
            label = result.stdout.split()[1]
            identifier = label[4:]
            self.assertEqual(uuid.UUID(identifier).version, 4)
            # UUIDs are random; the filename's private prefix records FIFO order.
            paths = list(self.runtime.glob(f'job-*-{identifier}.*'))
            self.assertEqual(len(paths), 1)
            return paths[0].stem, f'text-{index}'
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            submissions = list(pool.map(submit, range(12)))
        self.assertEqual(len({job for job, _ in submissions}), 12)
        (self.root / 'release').touch()
        self.wait_for_order([text for _, text in sorted(submissions)])

    def test_queue_full(self):
        self.script.write_text(self.script.read_text().replace('QUEUE_LIMIT=64', 'QUEUE_LIMIT=2'))
        self.start_worker(GATE='1')
        self.assertEqual(self.run_script(['123', 'active']).returncode, 0)
        deadline = time.monotonic() + 3
        while not list(self.runtime.glob('job-*.running')) and time.monotonic() < deadline:
            time.sleep(0.02)
        for text in ['waiting-1', 'waiting-2']:
            self.assertEqual(self.run_script(['123', text]).returncode, 0)
        result = self.run_script(['123', 'overflow'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Queue is full', result.stderr)

    def test_worker_down(self):
        result = self.run_script(['123', 'hello'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not running', result.stderr)
        self.assertEqual(list(self.runtime.glob('job-*.json')), [])

    def test_text_limit(self):
        result = self.run_script(['123', 'x' * 16385])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UTF-8 bytes', result.stderr)

    def test_failure_does_not_stall_queue(self):
        self.start_worker(PIPER_EXIT='7')
        for text in ['first', 'second']:
            self.assertEqual(self.run_script(['123', text]).returncode, 0)
        self.wait_for_order(['first', 'second'])
        self.assertFalse((self.root / 'playback').exists())

    def test_interrupted_job_is_not_replayed(self):
        (self.runtime / f'job-00000000000000000001-{uuid.uuid4()}.running').write_text('{}')
        self.start_worker()
        self.assertFalse(list(self.runtime.glob('*.running')))
        self.assertIn('Discarding interrupted job', (self.root / 'worker.log').read_text())

    def test_duplicate_worker_is_rejected(self):
        self.start_worker()
        result = self.run_script(['--worker'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already running', result.stderr)

    def test_setup_service_failure(self):
        result = self.run_script(['--setup'], SYSTEMCTL_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Setup complete', result.stdout)

    def test_setup_root_fallback(self):
        result = self.run_script(['--setup'], NO_ASTERISK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('User=root', self.unit.read_text())
        self.assertIn('Group=0', self.unit.read_text())
        self.assertIn('uid=0,gid=0', self.fstab.read_text())

    def test_pending_jobs_are_recovered(self):
        for order, text in [(2, 'second'), (1, 'first')]:
            (self.runtime / f'job-{order:020d}-{uuid.uuid4()}.json').write_text(
                json.dumps({'node': '123', 'text': text}))
        self.start_worker()
        self.wait_for_order(['first', 'second'])

    def test_finished_jobs_and_audio_are_removed_without_counter(self):
        self.start_worker()
        result = self.run_script(['123', 'hello'])
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = result.stdout.split()[1][4:]
        self.assertEqual(uuid.UUID(identifier).version, 4)
        self.wait_for_order(['hello'])
        deadline = time.monotonic() + 3
        while list(self.runtime.glob('job-*')) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(list(self.runtime.glob('job-*')), [])
        self.assertFalse((self.runtime / 'sequence').exists())
        self.assertFalse(list(self.runtime.glob('.publish-*')))
        self.assert_clean()

    def test_job_timeout_does_not_stall_queue(self):
        self.script.write_text(self.script.read_text().replace('JOB_TIMEOUT=600', 'JOB_TIMEOUT=1'))
        self.start_worker(GATE='1')
        for text in ['first', 'second']:
            self.assertEqual(self.run_script(['123', text]).returncode, 0)
        deadline = time.monotonic() + 4
        while 'exit=124' not in (self.root / 'worker.log').read_text():
            if time.monotonic() >= deadline:
                self.fail('Job timeout did not fire')
            time.sleep(0.02)
        (self.root / 'release').touch()
        self.wait_for_order(['second'])

    def test_queue_file_permissions(self):
        self.start_worker(GATE='1')
        result = self.run_script(['123', 'hello'])
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.runtime.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o660)
            self.assertEqual(path.stat().st_gid, self.runtime.stat().st_gid)

    def test_setup_installs_missing_tts_package(self):
        result = self.run_script(['--setup'], PACKAGE_MISSING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'apt_calls').read_text().splitlines(),
                         ['update', 'install -y asl3-tts'])
        # A second setup does not update or install an already installed package.
        result = self.run_script(['--setup'], PACKAGE_MISSING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((self.root / 'apt_calls').read_text().splitlines()), 2)

    def test_setup_skips_installed_tts_package(self):
        self.assertEqual(self.run_script(['--setup']).returncode, 0)
        self.assertFalse((self.root / 'apt_calls').exists())

    def test_setup_package_update_failure(self):
        before = self.fstab.read_text()
        result = self.run_script(['--setup'], PACKAGE_MISSING='1', UPDATE_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'apt_calls').read_text().splitlines(), ['update'])
        self.assertEqual(self.fstab.read_text(), before)
        self.assertFalse(self.unit.exists())

    def test_setup_package_install_failure(self):
        result = self.run_script(['--setup'], PACKAGE_MISSING='1', INSTALL_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot install asl3-tts', result.stderr)
        self.assertFalse(self.unit.exists())

    def test_setup_checks_voice_before_installing_service(self):
        self.voice.unlink()
        result = self.run_script(['--setup'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Voice model or metadata unreadable', result.stderr)
        self.assertFalse(self.unit.exists())


if __name__ == '__main__':
    unittest.main()
