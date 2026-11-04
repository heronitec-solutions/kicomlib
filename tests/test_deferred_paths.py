"""Regression coverage for KiCad overwriting externally edited path settings."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from KiComLibClient import DeferredPaths as deferred
from KiComLibClient import KiCadSettings as settings


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / 'kicad_common.json'
    path.write_text(json.dumps({'environment': {'vars': {'USER_PATH': '/user'}}, 'other': 1}))
    monkeypatch.setattr(settings, 'GetKiCadSettingsCommonSettingsFile', lambda: str(path))
    monkeypatch.setattr(settings, 'pcbnew', object())
    monkeypatch.setattr(deferred, 'python_executable', lambda: sys.executable)
    monkeypatch.setattr(deferred.subprocess, 'Popen', lambda *args, **kwargs: None)
    monkeypatch.setattr(deferred, 'kicad_running', lambda: False)
    return path


def test_changes_survive_stale_kicad_save_and_preserve_latest_settings(config):
    stale = config.read_bytes()
    settings.ensureKiCadEnvVars('EKA', '/library')
    assert config.read_bytes() == stale  # The plugin must not edit KiCad's live file.
    # KiCad saves an unrelated setting from its old in-memory configuration.
    data = json.loads(stale)
    data['other'] = 2
    config.write_text(json.dumps(data))
    assert deferred.apply_pending(config)
    saved = json.loads(config.read_text())
    assert saved['other'] == 2
    assert saved['environment']['vars']['USER_PATH'] == '/user'
    assert saved['environment']['vars']['EKA_BASE_PATH'] == '/library'
    assert len(saved['environment']['vars']) == 7
    assert not list(deferred.pending_changes(config))
    backup = Path(str(config) + '.before-kicomlib-paths.bak')
    assert json.loads(backup.read_text()) == data


def test_add_rename_move_remove_before_shutdown(config):
    assert settings.addKiCadEnvVars('OLD', '/old')
    assert not settings.addKiCadEnvVars('OLD', '/ignored')
    settings.renameKiCadEnvVars('OLD', 'NEW')
    settings.updateKiCadEnvVars('NEW', '/new')
    pending = deferred.read_variables(config)
    assert pending['NEW_BASE_PATH'] == '/new'
    assert not any(key.startswith('OLD_') for key in pending)
    settings.removeKiCadEnvVars('NEW')
    assert deferred.apply_pending(config)
    assert json.loads(config.read_text())['environment']['vars'] == {'USER_PATH': '/user'}


def test_ensure_is_idempotent_and_corrects_pending_paths(config):
    assert len(settings.ensureKiCadEnvVars('EKA', '/old')) == 6
    assert settings.ensureKiCadEnvVars('EKA', '/old') == []
    assert len(settings.ensureKiCadEnvVars('EKA', '/new')) == 6
    assert deferred.apply_pending(config)
    assert json.loads(config.read_text())['environment']['vars']['EKA_BASE_PATH'] == '/new'


def test_worker_never_writes_while_kicad_is_running(config, monkeypatch):
    settings.addKiCadEnvVars('EKA', '/library')
    original = config.read_bytes()
    monkeypatch.setattr(deferred, 'kicad_running', lambda: True)
    assert not deferred.apply_pending(config)
    assert config.read_bytes() == original
    assert list(deferred.pending_changes(config))


def test_failed_replace_keeps_pending_and_original(config, monkeypatch):
    settings.addKiCadEnvVars('EKA', '/library')
    original = config.read_bytes()
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(deferred.os, 'replace', fail)
    with pytest.raises(OSError, match='disk full'):
        deferred.apply_pending(config)
    assert config.read_bytes() == original
    assert list(deferred.pending_changes(config))


def test_spawn_failure_keeps_journal(config, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('cannot spawn')
    monkeypatch.setattr(deferred.subprocess, 'Popen', fail)
    with pytest.raises(RuntimeError, match='could not start'):
        settings.addKiCadEnvVars('EKA', '/library')
    assert list(deferred.pending_changes(config))


def test_process_enumeration_failure_does_not_apply(config, monkeypatch):
    settings.addKiCadEnvVars('EKA', '/library')
    original = config.read_bytes()
    def fail():
        raise subprocess.CalledProcessError(1, 'ps')
    monkeypatch.setattr(deferred, 'kicad_running', fail)
    with pytest.raises(subprocess.CalledProcessError):
        deferred.apply_pending(config)
    assert config.read_bytes() == original
    assert list(deferred.pending_changes(config))


@pytest.mark.parametrize('name', ['kicad', 'eeschema', '/Applications/KiCad/pcbnew', 'gerbview'])
def test_process_detection(name, monkeypatch):
    if os.name == 'nt':
        output = f'"{Path(name).name}.exe","123","Console","1","100 K"\n'
    else:
        output = 'python3\n' + name + '\n'
    monkeypatch.setattr(deferred.subprocess, 'check_output', lambda *args, **kwargs: output)
    assert deferred.kicad_running()


def test_real_kicad_save_cannot_erase_journal(tmp_path):
    if importlib.util.find_spec('pcbnew') is None:
        pytest.skip('KiCad Python bindings are not installed')
    # Use a fresh process and an isolated configuration; never load real user settings.
    directory = tmp_path / '10.0'
    directory.mkdir()
    config = directory / 'kicad_common.json'
    config.write_text(json.dumps({'environment': {'vars': {}}}))
    script = '''
import json
from pathlib import Path
import pcbnew
from KiComLibClient import KiCadSettings as settings
from KiComLibClient import DeferredPaths as deferred
manager = pcbnew.GetSettingsManager()
config = Path(settings.GetKiCadSettingsCommonSettingsFile())
assert config.is_relative_to(Path(__import__('os').environ['KICAD_CONFIG_HOME']))
# Do not spawn a worker during the regression test; apply it after this process exits.
deferred.python_executable = lambda: __import__('sys').executable
deferred.subprocess.Popen = lambda *args, **kwargs: None
settings.ensureKiCadEnvVars('REGRESSION', '/library')
manager.Save()
assert 'REGRESSION_BASE_PATH' not in json.loads(config.read_text())['environment']['vars']
assert deferred.read_variables(config)['REGRESSION_BASE_PATH'] == '/library'
'''
    env = dict(os.environ, KICAD_CONFIG_HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path))
    subprocess.run([sys.executable, '-c', script], env=env, check=True, timeout=30)
    # The test host may have unrelated KiCad windows open. Only this isolated test's
    # KiCad process matters here, and subprocess.run has already waited for its exit.
    from unittest.mock import patch
    with patch.object(deferred, 'kicad_running', return_value=False):
        assert deferred.apply_pending(config)
    assert json.loads(config.read_text())['environment']['vars']['REGRESSION_BASE_PATH'] == '/library'


def test_worker_environment_excludes_embedded_python_paths(monkeypatch):
    monkeypatch.setenv('PYTHONHOME', '/embedded/python')
    monkeypatch.setenv('PYTHONPATH', '/embedded/modules')
    monkeypatch.setenv('KICAD_CONFIG_HOME', '/custom/settings')
    environment = deferred.worker_environment()
    assert 'PYTHONHOME' not in environment
    assert 'PYTHONPATH' not in environment
    assert environment['KICAD_CONFIG_HOME'] == '/custom/settings'


def test_detached_worker_applies_changes(tmp_path):
    import time
    if deferred.kicad_running():
        pytest.skip('Close KiCad for the detached-worker integration test')
    config = tmp_path / 'kicad_common.json'
    config.write_text(json.dumps({'environment': {'vars': {'USER': '/user'}}}))
    deferred.queue_changes(config, {'DEMO_BASE_PATH': '/old'})
    deferred.queue_changes(config, {'DEMO_BASE_PATH': '/library'})
    deferred.queue_changes(config, {'SECOND_BASE_PATH': '/second'})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        data = json.loads(config.read_text())
        if (data['environment']['vars'].get('DEMO_BASE_PATH') == '/library'
                and data['environment']['vars'].get('SECOND_BASE_PATH') == '/second'
                and not list(deferred.pending_dir(config).glob('*.json'))):
            assert data['environment']['vars']['USER'] == '/user'
            return
        time.sleep(0.05)
    pytest.fail('Detached worker did not finish: ' +
                (deferred.pending_dir(config) / 'worker.log').read_text())
