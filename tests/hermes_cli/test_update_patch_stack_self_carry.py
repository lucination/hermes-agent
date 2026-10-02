"""A real updater and vault patch survive two updates in fresh Python processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from tests.hermes_cli.test_update_patch_stack_git import git, commit

ROOT = Path(__file__).resolve().parents[2]


def run_checkout(root, home, action):
    # Only dependency/build/restart effects are seams: the CLI parser, command
    # boundary, native dispatch, Git staging/publication and imports are real.
    script = r'''
import json, sys
from types import SimpleNamespace
sys.argv = ['hermes', *ARGV]
from hermes_cli import main, update_cmd as uc, update_patch_stack as stack
from hermes_cli.config import load_config
from hermes_cli.update_receipt import begin_update_receipt
main._install_hangup_protection = lambda **kw: None
main._finalize_update_output = lambda state: None
main._run_pre_update_backup = lambda args: None
main._installed_desktop_apps = lambda: []
uc._resolve_update_options = lambda *a: SimpleNamespace()
uc._begin_update_receipt_and_plan = lambda args: begin_update_receipt()
uc._source_completion_request = lambda *a: {'windows_resume': None}

def completion(request):
    assert request['expected_sha'] == stack.sha(main.PROJECT_ROOT, 'HEAD')
    assert request['branch'] == 'lucination'
    assert request['apply_mode'] == 'patch-stack'
    print('COMPLETION ' + json.dumps(request))
uc._complete_source_update = completion
main.main()
from agent import vault_store
assert str(stack.__file__).startswith(str(main.PROJECT_ROOT))
assert str(vault_store.__file__).startswith(str(main.PROJECT_ROOT))
print('PROOF ' + json.dumps({'updater': stack.__file__, 'vault': vault_store.__file__,
    'head': stack.sha(main.PROJECT_ROOT, 'HEAD'),
    'base': stack.sha(main.PROJECT_ROOT, stack.base_ref(main.PROJECT_ROOT)),
    'pid': __import__('os').getpid()}))
'''.replace('ARGV', repr(action))
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'HERMES_HOME': str(home),
           'TMPDIR': str(home), 'TMP': str(home), 'TEMP': str(home),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
    return subprocess.run([sys.executable, '-B', '-c', script], cwd=root,
                          env=env, capture_output=True, text=True, timeout=90)


def test_real_updater_and_vault_self_carry_across_two_fresh_process_updates(tmp_path):
    upstream = tmp_path / 'upstream.git'
    git(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(upstream))
    writer = tmp_path / 'writer'
    git(tmp_path, 'clone', '-q', str(upstream), str(writer))
    # Copy source, never runtime state or credentials. Include new implementation
    # files that are not tracked yet; no synthetic startup modules are substituted.
    names = git(ROOT, 'ls-files').splitlines()
    names += [str(p.relative_to(ROOT)) for p in (ROOT / 'hermes_cli').rglob('*.py')]
    for name in set(names):
        source = ROOT / name
        if not source.is_file() or source.is_symlink():
            continue
        if not (name.endswith('.py') or name in ('.gitignore', 'pyproject.toml', 'pm/lock.json')):
            continue
        if name.startswith(('tests/', 'skills/', 'optional-skills/', 'evals/')):
            continue
        dest = writer / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    carried = ('hermes_cli/update_patch_stack.py', 'agent/vault_store.py')
    contents = {name: (writer / name).read_text() for name in carried}
    for name in carried:
        (writer / name).unlink()
    git(writer, 'add', '.')
    base = commit(writer, 'upstream-generation.txt', 'one\n')
    git(writer, 'push', '-q', 'origin', 'main')
    fork = tmp_path / 'fork.git'
    git(tmp_path, 'clone', '-q', '--bare', str(upstream), str(fork))
    live = tmp_path / 'install'
    git(tmp_path, 'clone', '-q', str(fork), str(live))
    git(live, 'remote', 'add', 'upstream', str(upstream))
    git(live, 'fetch', '-q', 'upstream')
    git(live, 'config', 'user.name', 'Fixture')
    git(live, 'config', 'user.email', 'fixture@example.invalid')
    git(live, 'checkout', '-qb', 'lucination')
    for name in carried:
        commit(live, name, contents[name])
    original = git(live, 'rev-parse', 'HEAD')
    home = tmp_path / 'home'
    home.mkdir()
    setup = run_checkout(live, home, ['update', '--set-patch-stack', 'lucination', '--base', 'upstream/main'])
    assert setup.returncode == 0, setup.stdout + setup.stderr
    assert git(live, 'rev-parse', 'HEAD') == original
    pids = []
    previous = original
    for generation in ('two', 'three'):
        target = commit(writer, 'upstream-generation.txt', generation + '\n')
        git(writer, 'push', '-q', 'origin', 'main')
        result = run_checkout(live, home, ['update', '--yes', '--no-backup', '--no-gateway-restart'])
        assert result.returncode == 0, result.stdout + result.stderr + git(live, 'status', '--porcelain')
        evidence = json.loads(next(line.removeprefix('PROOF ') for line in result.stdout.splitlines() if line.startswith('PROOF ')))
        pids.append(evidence['pid'])
        assert evidence['base'] == target
        assert evidence['head'] == git(live, 'rev-parse', 'HEAD') != previous
        assert 'COMPLETION ' in result.stdout
        assert git(live, 'branch', '--show-current') == 'lucination'
        assert git(live, 'rev-list', '--count', f'{target}..HEAD') == '2'
        assert git(live, 'rev-list', '--merges', f'{target}..HEAD') == ''
        assert git(live, 'status', '--porcelain') == ''
        for name in carried:
            assert (live / name).read_text() == contents[name]
        previous = evidence['head']
    assert len(set(pids)) == 2
    assert git(upstream, 'rev-parse', 'main') == target
    assert git(fork, 'rev-parse', 'main') == base
    assert git(fork, 'show-ref', '--verify', 'refs/heads/lucination', check=False) == ''
    assert git(upstream, 'show-ref', '--verify', 'refs/heads/lucination', check=False) == ''
    assert base != target
