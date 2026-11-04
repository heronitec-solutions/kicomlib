"""Persist path changes after KiCad exits. Also runnable as a stdlib-only worker.

Never import pcbnew here: that would create another stale settings manager.
"""
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager


KICAD_PROGRAMS = {
    'kicad', 'pcbnew', 'eeschema', 'cvpcb', 'gerbview', 'pl_editor',
    'pcb_calculator', 'bitmap2component', 'kicad-cli', 'kipython',
}


def atomic_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=4)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pending_dir(config):
    return Path(config).parent / '.kicomlib-pending-paths'


def pending_changes(config):
    """Replay pending changes when another edit happens before shutdown."""
    for path in sorted(pending_dir(config).glob('*.json')):
        yield path, json.loads(path.read_text(encoding='utf-8'))


def apply_changes(variables, changes):
    for key, value in changes.items():
        if value is None:
            variables.pop(key, None)
        else:
            variables[key] = value


def read_variables(config):
    data = json.loads(Path(config).read_text(encoding='utf-8'))
    environment = data.get('environment')
    if not isinstance(environment, dict):
        environment = {}
    variables = environment.get('vars')
    variables = dict(variables) if isinstance(variables, dict) else {}
    for _, changes in pending_changes(config):
        apply_changes(variables, changes)
    return variables


def worker_environment():
    # Do not pass an embedded Python's installation paths to a system interpreter.
    environment = dict(os.environ)
    environment.pop('PYTHONHOME', None)
    environment.pop('PYTHONPATH', None)
    return environment


def python_executable():
    """sys.executable can be pcbnew in KiCad's embedded Python."""
    candidates = [sys.executable]
    for prefix in (sys.prefix, sys.base_prefix):
        candidates.extend(str(Path(prefix) / name) for name in
                          ('bin/python3', 'bin/python', 'python.exe', 'bin/python.exe'))
    candidates.extend(shutil.which(name) for name in ('python3', 'python'))
    for candidate in dict.fromkeys(candidates):
        if not candidate or not Path(candidate).name.lower().startswith('python'):
            continue
        try:
            result = subprocess.run(
                [candidate, '-c', 'import sys; print(sys.version_info >= (3, 9))'],
                capture_output=True, text=True, timeout=5, env=worker_environment(),
            )
            if result.returncode == 0 and result.stdout.strip() == 'True':
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError('No Python 3.9+ interpreter found for deferred KiCad path setup. '
                       'Install Python and put python3 or python on PATH, then retry.')


def queue_changes(config, changes):
    """One durable file per edit; workers merge changes into the latest settings."""
    executable = python_executable()
    directory = pending_dir(config)
    directory.mkdir(exist_ok=True)
    path = directory / f'{time.time_ns():020d}-{uuid.uuid4().hex}.json'
    atomic_json(path, changes)
    options = {'start_new_session': True} if os.name != 'nt' else {
        'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    }
    try:
        with (directory / 'worker.log').open('ab') as log:
            subprocess.Popen(
                [executable, str(Path(__file__).resolve()), str(Path(config).resolve())],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                env=worker_environment(), **options,
            )
    except OSError as exc:
        # Keep the journal so the next successful worker can replay it.
        raise RuntimeError(f'Path changes are queued in {directory}, but the setup worker '
                           f'could not start: {exc}') from exc
    print('KiCad path changes queued. Close all KiCad windows and editors, wait for '
          'setup to finish, then reopen KiCad. Status: ' + str(directory / 'worker.log'))


def kicad_running():
    """Fail closed if process enumeration fails. Include all users, conservatively."""
    if os.name == 'nt':
        output = subprocess.check_output(['tasklist', '/FO', 'CSV', '/NH'], text=True, timeout=10)
        names = [row[0] for row in csv.reader(io.StringIO(output)) if row]
    else:
        output = subprocess.check_output(['ps', '-A', '-o', 'comm='], text=True, timeout=10)
        names = output.splitlines()
    for name in names:
        name = Path(name.strip()).name.lower()
        if name.endswith('.exe'):
            name = name[:-4]
        if name in KICAD_PROGRAMS:
            return True
    return False


@contextmanager
def worker_lock(directory):
    # Serialize workers from different KiCad processes as well as repeated edits.
    with (directory / 'worker.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.write(b'\0')
            stream.flush()
            stream.seek(0)
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def apply_pending(config):
    """Called only by the independent worker, after all KiCad processes exit."""
    config = Path(config)
    with worker_lock(pending_dir(config)):
        # Recheck after locking: another worker or a new KiCad session may have started.
        if kicad_running():
            return False
        pending = list(pending_changes(config))
        if not pending:
            return True
        original = config.read_bytes()
        data = json.loads(original)
        if not isinstance(data.get('environment'), dict):
            data['environment'] = {}
        if not isinstance(data['environment'].get('vars'), dict):
            data['environment']['vars'] = {}
        for _, changes in pending:
            apply_changes(data['environment']['vars'], changes)
        if kicad_running() or config.read_bytes() != original:
            return False
        shutil.copy2(config, str(config) + '.before-kicomlib-paths.bak')
        atomic_json(config, data)
        for path, _ in pending:
            path.unlink()
        print('KiComLib path setup complete. You can reopen KiCad.', flush=True)
        return True


def run_worker(config):
    print('Waiting for all KiCad windows and editors to close...', flush=True)
    while True:
        # Enumeration errors abort without touching the settings or dropping the journal.
        if not kicad_running() and apply_pending(config):
            return
        time.sleep(1)


if __name__ == '__main__':
    run_worker(sys.argv[1])
