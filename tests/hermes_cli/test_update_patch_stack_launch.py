"""Early admission must reach policy commands without legacy launch mutations."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def launch(tmp_path, args, *, configured=False, main=False, no_tmp=False):
    from hermes_cli.update_channel import install_id
    home = tmp_path / 'home'
    home.mkdir()
    if configured:
        policy = dict(branch='patches', base_remote='upstream', base_branch='main',
                      remote_url='https://example.invalid/upstream.git', anchor_sha='a' * 40)
        (home / 'config.yaml').write_text(json.dumps({'update': {'installs': {
            install_id(ROOT): {'channel': 'main', 'patch_stack': policy}}}}))
    script = '''
import sys
from hermes_cli import venv_sync, _early_recovery
from pm import environments

def forbidden(*args, **kw):
    raise AssertionError('legacy launch mutation')
venv_sync.prepare_launch = forbidden
_early_recovery.recover_if_needed = forbidden
_early_recovery.restore_interrupted_pull = forbidden
environments.activate_dependencies = forbidden
sys.argv = ['hermes', *ARGS]
import hermes_bootstrap
if MAIN:
    from hermes_cli import update_cmd_check
    update_cmd_check.report_patch_stack = lambda root, policy: print('readonly report')
    import hermes_cli.main
print('admitted')
'''.replace('ARGS', repr(args)).replace('MAIN', repr(main))
    env = {**os.environ, 'HOME': str(home), 'HERMES_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}
    if no_tmp:
        for key in ('TMPDIR', 'TMP', 'TEMP', 'HERMES_SCRATCH_DIR'):
            env.pop(key, None)
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT, env=env,
                            text=True, capture_output=True, timeout=60)
    return result, home


@pytest.mark.parametrize('configured', [False, True])
@pytest.mark.parametrize('worktree', [False, True])
@pytest.mark.parametrize('completion', [False, True])
def test_pending_plain_yes_bootstrap_refuses_before_stale_repair(tmp_path, configured, worktree, completion):
    import shutil
    from hermes_cli.update_channel import install_id
    root = tmp_path / 'checkout'
    root.mkdir()
    common = root / '.git'
    common.mkdir()
    if worktree:
        common = tmp_path / 'common'
        common.mkdir()
        gitdir = common / 'worktrees' / 'fixture'
        gitdir.mkdir(parents=True)
        (gitdir / 'commondir').write_text('../..')
        (root / '.git').rmdir()
        (root / '.git').write_text(f'gitdir: {gitdir}\n')
    journals = common / 'hermes-patch-stacks'
    journals.mkdir()
    journal = journals / 'sibling-install.json'
    journal_text = json.dumps({'phase': 'completion' if completion else 'configure'})
    journal.write_text(journal_text)
    (root / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (root / 'install-stamp.json').write_text('{"updateMechanism": "self"}')
    shutil.copyfile(ROOT / 'hermes_bootstrap.py', root / 'hermes_bootstrap.py')
    home = tmp_path / 'home'
    home.mkdir()
    if configured:
        policy = dict(branch='patches', base_remote='upstream', base_branch='main',
                      remote_url='https://example.invalid/base', anchor_sha='a' * 40)
        (home / 'config.yaml').write_text(json.dumps({'update': {'installs': {
            install_id(root): {'patch_stack': policy}}}}))
    script = f"""
import sys
from pathlib import Path
import pm
from hermes_cli import venv_sync, _early_recovery
from pm import environments
pm.venv_is_current = lambda **kw: False
from hermes_cli import steward
steward.read_install_stamp = lambda root: {{'updateMechanism': 'self'}}
def forbidden(*a, **kw):
    raise AssertionError('pending transaction reached legacy repair')
venv_sync._finish_source_update = forbidden
_early_recovery.recover_if_needed = forbidden
_early_recovery.restore_interrupted_pull = forbidden
environments.activate_dependencies = (lambda root: print('activation only')) if {completion!r} else forbidden
sys.argv = ['hermes', 'gateway', 'run'] if {completion!r} else ['hermes', 'update', '--yes']
import importlib.util
spec = importlib.util.spec_from_file_location('hermes_bootstrap', {str(root / 'hermes_bootstrap.py')!r})
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
assert venv_sync.prepare_launch(Path({str(root)!r}), sys.argv[1:]) is None
print('pending admitted for explicit dispatch refusal')
"""
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT,
                            env={**os.environ, 'HERMES_HOME': str(home)},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert journal.read_text() == journal_text
    if completion:
        assert 'activation only' in result.stdout


def test_configure_admitted_before_launch_mutations(tmp_path):
    result, _ = launch(tmp_path, ['update', '--set-patch-stack', 'patches', '--base', 'upstream/main'])
    assert result.returncode == 0, result.stderr
    assert 'admitted' in result.stdout


@pytest.mark.parametrize('flag', ['--check', '--plan'])
def test_configured_preview_admitted_before_launch_mutations(tmp_path, flag):
    result, _ = launch(tmp_path, ['update', flag], configured=True)
    assert result.returncode == 0, result.stderr


def test_no_policy_retains_legacy_launch(tmp_path):
    result, _ = launch(tmp_path, ['update', '--check'])
    assert result.returncode != 0
    assert 'legacy launch mutation' in result.stderr


def test_admission_does_not_create_or_prune_scratch(tmp_path):
    result, home = launch(tmp_path, ['update', '--check'], configured=True, main=True, no_tmp=True)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in home.iterdir()) == ['config.yaml']


def test_main_configured_preview_does_not_restore_or_open_logs(tmp_path):
    result, home = launch(tmp_path, ['update', '--plan'], configured=True, main=True)
    assert result.returncode == 0, result.stderr
    assert not (home / 'logs').exists(), result.stderr


@pytest.mark.parametrize('args', [
    ['update', '--set-patch-stack', 'patches', '--base', 'upstream/main'],
    ['update', '--clear-patch-stack'],
    ['update', '--check'], ['update', '--plan'],
])
def test_prepare_launch_leaves_stale_dependencies_and_pending_markers(tmp_path, args):
    from hermes_cli.update_channel import install_id
    root = tmp_path / 'checkout'
    root.mkdir()
    (root / '.git').mkdir()
    (root / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (root / 'install-stamp.json').write_text(json.dumps({'updateMechanism': 'self'}))
    home = tmp_path / 'profile'
    home.mkdir()
    policy = dict(branch='patches', base_remote='upstream', base_branch='main',
                  remote_url='https://example.invalid/base', anchor_sha='a' * 40)
    config = home / 'config.yaml'
    config.write_text(json.dumps({'update': {'installs': {install_id(root): {'patch_stack': policy}}}}))
    pending = home / 'source-completion-pending'
    pending.write_text('unfinished')
    script = f'''
from pathlib import Path
import pm
from hermes_cli import venv_sync
root = Path({str(root)!r})
pm.venv_is_current = lambda **kw: False
venv_sync.completion_pending_path = lambda root: Path({str(pending)!r})
def forbidden(*a, **kw):
    raise AssertionError('stale dependency repair ran')
venv_sync._finish_source_update = forbidden
assert venv_sync.prepare_launch(root, {args!r}) is None
'''
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT,
                            env={**os.environ, 'HERMES_HOME': str(home)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
