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
real_activate = environments.activate_dependencies
activation_calls = []
def activate(root, *, read_only=False):
    assert read_only
    activation_calls.append(root)
    assert len(activation_calls) == 1
    return real_activate(root, read_only=read_only)
environments.activate_dependencies = activate
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
environments.activate_dependencies = (lambda root, *, read_only=False: print('activation only') if not read_only else forbidden()) if {completion!r} else forbidden
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


@pytest.mark.skipif(os.name == 'nt', reason='readiness waits on POSIX subprocess pipes')
@pytest.mark.parametrize('activation', ['reserved-service', 'default'])
def test_running_service_retains_generation_after_completion_and_selection_change(tmp_path, activation):
    """The booted generation remains available for lazy imports until process exit."""
    import select
    import shutil
    from pm.environments import install_key

    root = tmp_path / 'checkout'
    root.mkdir()
    shutil.copyfile(ROOT / 'hermes_bootstrap.py', root / 'hermes_bootstrap.py')
    journal = root / '.git/hermes-patch-stacks/reservation.json'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'phase': 'completion'}))
    home = tmp_path / 'home'
    state = home / 'installs' / install_key(root)
    generations = state / 'environments'
    old = generations / 'old'
    new = generations / 'new'
    for generation in (old, new):
        environment = generation / 'venv'
        environment.mkdir(parents=True)
        (generation / '.lease-managed').touch()
        (environment / 'pyvenv.cfg').write_text(
            f'version = {sys.version_info.major}.{sys.version_info.minor}.0\n')
        packages = environment / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        packages.mkdir(parents=True)
        (packages / 'late_import.py').write_text(f'GENERATION = {generation.name!r}\n')
    facts = state / 'facts.json'
    facts.write_text(json.dumps({'packages': {'venv': {'environment': str(old / 'venv')}}}))
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'HERMES_HOME': str(home),
           'TMPDIR': str(tmp_path), 'TMP': str(tmp_path), 'TEMP': str(tmp_path)}
    script = f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from pm import environments
from hermes_cli import venv_sync, _early_recovery

def forbidden(*a, **kw):
    raise AssertionError('reserved service reached legacy launch repair')
venv_sync.prepare_launch = forbidden
_early_recovery.recover_if_needed = forbidden
if {activation!r} == 'reserved-service':
    import importlib.util
    sys.argv = ['hermes', 'gateway', 'run']
    spec = importlib.util.spec_from_file_location('hermes_bootstrap', {str(root / 'hermes_bootstrap.py')!r})
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
else:
    environments.activate_dependencies(Path({str(root)!r}))
print('ready', flush=True)
sys.stdin.readline()
import late_import
assert late_import.GENERATION == 'old'
print('old generation still usable', flush=True)
'''
    collect_script = f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from hermes_cli.runtime_state import collect_generations
print([p.name for p in collect_generations(Path({str(root)!r}), min_age_seconds=0)])
'''
    child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', script], cwd=tmp_path,
                             env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 20)[0], 'service failed to become ready'
        assert child.stdout.readline().strip() == 'ready'
        journal.unlink()  # Completion finishes while the service continues running.
        facts.write_text(json.dumps({'packages': {'venv': {'environment': str(new / 'venv')}}}))
        collected = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', collect_script],
                                   cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
        assert collected.returncode == 0, collected.stderr
        assert old.is_dir(), f'running service generation collected: {collected.stdout}'
        assert list((old / '.leases').iterdir()), 'service did not acquire a generation lease'
        stdout, stderr = child.communicate('\n', timeout=20)
        assert child.returncode == 0, stderr
        assert 'old generation still usable' in stdout
        collected = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', collect_script],
                                   cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
        assert collected.returncode == 0, collected.stderr
        assert not old.exists(), 'exited service generation was not collected'
        assert new.is_dir()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=20)


@pytest.mark.parametrize('mode', ['configure', '--plan', '--check'])
def test_cold_policy_command_activates_selected_dependencies_without_pm_writes(tmp_path, mode):
    """A base Python has no ruamel; only the committed PM generation provides it."""
    import shutil
    import sysconfig
    from pm.environments import install_key

    root = tmp_path / 'checkout'
    root.mkdir()
    shutil.copytree(ROOT / 'hermes_cli', root / 'hermes_cli', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(ROOT / 'hermes_bootstrap.py', root / 'hermes_bootstrap.py')
    for source in ROOT.iterdir():
        if source.name not in {'hermes_cli', 'hermes_bootstrap.py', '.git', '.hermes', 'venv', '.venv'}:
            (root / source.name).symlink_to(source, target_is_directory=source.is_dir())
    home = tmp_path / 'home'
    home.mkdir()
    state = home / 'installs' / install_key(root)
    generation = state / 'environments' / 'selected'
    generation.mkdir(parents=True)
    (generation / 'pyvenv.cfg').write_text(f'version = {sys.version_info.major}.{sys.version_info.minor}.0\n')
    selected = generation / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
    selected.parent.mkdir(parents=True)
    selected.symlink_to(sysconfig.get_path('purelib'), target_is_directory=True)
    (state / 'facts.json').write_text(json.dumps({'packages': {'venv': {'environment': str(generation)}}}))
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'HERMES_HOME': str(home),
           'TMPDIR': str(tmp_path), 'TMP': str(tmp_path), 'TEMP': str(tmp_path)}
    def git(*args, cwd=root):
        return subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True,
                              text=True, check=True).stdout.strip()
    git('init', '-b', 'patches')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', 'base')
    head = git('rev-parse', 'HEAD')
    upstream = tmp_path / 'upstream.git'
    git('clone', '--bare', str(root), str(upstream))
    git('update-ref', 'refs/heads/main', head, cwd=upstream)
    git('remote', 'add', 'upstream', str(upstream))
    git('update-ref', 'refs/remotes/upstream/main', head)
    from hermes_cli.update_channel import install_id
    from hermes_cli.update_patch_stack import base_ref
    git('update-ref', base_ref(root), head)
    if mode == 'configure':
        args = ['update', '--set-patch-stack', 'patches', '--base', 'upstream/main']
    else:
        policy = dict(branch='patches', base_remote='upstream', base_branch='main',
                      remote_url=str(upstream), anchor_sha=head)
        (home / 'config.yaml').write_text(json.dumps({'update': {'installs': {
            install_id(root): {'channel': 'main', 'patch_stack': policy}}}}))
        args = ['update', mode]
    script = f'''
import sys
sys.path.insert(0, {str(root)!r})
try:
    import ruamel.yaml
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('cold interpreter unexpectedly carries ruamel')
from hermes_cli import venv_sync, _early_recovery, runtime_state
import pm

def forbidden(*a, **kw):
    raise AssertionError('read-only admission mutated PM state')
venv_sync.prepare_launch = forbidden
_early_recovery.recover_if_needed = forbidden
_early_recovery.restore_interrupted_pull = forbidden
runtime_state.runtime_lock = forbidden
runtime_state.recover_publication = forbidden
runtime_state.lease_generation = forbidden
pm.sync_venv = forbidden
sys.argv = ['hermes', *{args!r}]
import hermes_cli.main
hermes_cli.main.main()
'''
    before = {str(p): p.read_bytes() for p in state.rglob('*') if p.is_file()}
    home_before = {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()}
    git_before = {str(p): p.read_bytes() for p in (root / '.git').rglob('*') if p.is_file()}
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    if mode == 'configure':
        import hermes_yaml
        from hermes_cli.update_patch_stack import read_policy
        policy = read_policy(hermes_yaml.safe_load((home / 'config.yaml').read_text()), root)
        assert policy is not None
        assert policy.branch == 'patches'
        assert policy.anchor_sha == head
    else:
        assert 'Patch' in result.stdout
        assert {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()} == home_before
        assert {str(p): p.read_bytes() for p in (root / '.git').rglob('*') if p.is_file()} == git_before
    assert {str(p): p.read_bytes() for p in state.rglob('*') if p.is_file()} == before


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
