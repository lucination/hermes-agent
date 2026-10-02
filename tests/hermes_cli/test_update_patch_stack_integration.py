"""Native orchestration against real local Git; external completion is a seam."""
import argparse
from types import SimpleNamespace
import pytest
from tests.hermes_cli.test_update_patch_stack_git import stack, git, commit


def args(**values):
    return SimpleNamespace(**values)


def fake_main(root):
    return SimpleNamespace(PROJECT_ROOT=root, _run_pre_update_backup=lambda a: None,
                           _desktop_packaged_executable=lambda p: None,
                           _desktop_dist_exists=lambda p: False, _installed_desktop_apps=lambda: [])

def test_native_dispatch_publishes_patch_head_before_fresh_completion(stack, monkeypatch):
    import json
    from hermes_cli import update_cmd as uc
    t = stack
    commit(t.writer, 'advance.txt', 'advance\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    monkeypatch.setattr(uc, '_m', lambda: fake_main(t.live))
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: None)
    monkeypatch.setattr(uc, '_resolve_update_options', lambda a, g: SimpleNamespace(assume_yes=True, no_gateway_restart=True, pre_update_version='fixture'))
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    seen = []
    checkpoint_journals = []
    def checkpoint():
        data = json.loads(t.p.journal_path(t.live).read_text())
        assert data['phase'] == 'applied'
        checkpoint_journals.append(data)
    monkeypatch.setattr(uc._completion_receipt, 'checkpoint_update_receipt', checkpoint)
    def complete(request):
        data = json.loads(t.p.journal_path(t.live).read_text())
        assert data == {**checkpoint_journals[0], 'phase': 'completion'}
        assert git(t.live, 'rev-parse', data['recovery_head']) == t.head
        assert git(t.live, 'rev-parse', data['recovery_base']) == t.base
        seen.append(request)
    monkeypatch.setattr(uc, '_complete_source_update', complete)
    assert uc.dispatch_patch_stack(args(yes=True, no_gateway_restart=True)) is True
    assert not t.p.journal_path(t.live).exists()
    assert seen[0]['expected_sha'] == git(t.live, 'rev-parse', 'HEAD')
    assert seen[0]['expected_sha'] != git(t.writer, 'rev-parse', 'HEAD')
    assert seen[0]['branch'] == 'lucination'
    assert seen[0]['apply_mode'] == 'patch-stack'
    assert git(t.live, 'rev-list', '--count', f'{t.p.sha(t.live, t.p.base_ref(t.live))}..HEAD') == '2'


@pytest.mark.parametrize('flag', ['branch', 'channel', 'set_channel', 'switch_branch', 'keep_stash'])
def test_policy_overrides_refuse_before_any_effect(stack, monkeypatch, flag):
    from hermes_cli import update_cmd as uc
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('options before refusal'))
    with pytest.raises(SystemExit) as error:
        uc.dispatch_patch_stack(args(**{flag: 'main' if flag in ('branch', 'channel', 'set_channel') else True}))
    assert error.value.code == 2

def test_parser_setup_is_configure_only_and_clear_keeps_head(stack, monkeypatch):
    from hermes_cli import update_cmd as uc
    from hermes_cli.subcommands.update import build_update_parser
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda a: None)
    setup = parser.parse_args(['update', '--set-patch-stack', 'lucination', '--base', 'upstream/main'])
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('configuration must not update'))
    assert uc.dispatch_patch_stack(setup)
    clear = parser.parse_args(['update', '--clear-patch-stack'])
    assert uc.dispatch_patch_stack(clear)
    assert git(stack.live, 'rev-parse', 'HEAD') == stack.head
    assert stack.p.read_policy(__import__('hermes_cli.config', fromlist=['load_config']).load_config(), stack.live) is None
    assert uc.dispatch_patch_stack(args()) is False


@pytest.mark.parametrize('argv', [
    ['--set-patch-stack', 'lucination'], ['--base', 'upstream/main'],
    ['--set-patch-stack', 'lucination', '--base', 'upstream/main', '--clear-patch-stack'],
    ['--set-patch-stack', 'lucination', '--base', 'upstream/main', '--check'],
    ['--clear-patch-stack', '--channel', 'main'],
])
def test_parser_rejects_mixed_configuration(argv):
    from hermes_cli.subcommands.update import build_update_parser
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda a: None)
    with pytest.raises(SystemExit):
        parser.parse_args(['update', *argv])


@pytest.mark.parametrize('mode', ['check', 'plan'])
def test_preview_preserves_git_config_and_cache(stack, monkeypatch, capsys, mode):
    from hermes_cli import update_cmd as uc, config
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('preview attempted apply'))
    before = git(stack.live, 'show-ref')
    content = config.get_config_path().read_bytes()
    cache = dict(config._RAW_CONFIG_CACHE)
    assert uc.dispatch_patch_stack(args(**{mode: True}))
    assert git(stack.live, 'show-ref') == before
    assert config.get_config_path().read_bytes() == content
    assert config._RAW_CONFIG_CACHE == cache
    output = capsys.readouterr().out
    assert 'lucination' in output and 'upstream/main' in output and stack.base in output
    assert 'fetch' in output and '2' in output


@pytest.mark.parametrize('failure', ['pm', 'restart'])
def test_published_code_is_durable_and_failures_are_partial(stack, monkeypatch, failure):
    from hermes_cli import update_cmd as uc, update_receipt as receipt
    monkeypatch.setattr(uc, '_m', lambda: fake_main(stack.live))
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: receipt.begin_update_receipt())
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace(assume_yes=True, no_gateway_restart=False, pre_update_version='fixture'))
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    def fail(request):
        published = receipt.read_latest_receipt()
        assert published and published['code_published'] is True
        assert published['patch_stack']['head'] == stack.head
        import json
        reservation = json.loads(stack.p.journal_path(stack.live).read_text())
        assert reservation['phase'] == 'completion'
        assert reservation['head'] == stack.head
        raise RuntimeError(failure)
    monkeypatch.setattr(uc, '_complete_source_update', fail)
    with receipt.update_receipt_scope():
        with pytest.raises(RuntimeError, match=failure):
            uc.dispatch_patch_stack(args())
    result = receipt.read_latest_receipt()
    assert result['outcome'] == 'partial'
    assert result['code_published'] is True

@pytest.mark.parametrize('point', ['request', 'fact', 'completion-reservation'])
@pytest.mark.parametrize('advance', [False, True])
def test_completion_preparation_failure_keeps_applied_barrier(stack, monkeypatch, point, advance):
    import json
    from hermes_cli import update_cmd as uc, update_receipt as receipt
    if advance:
        commit(stack.writer, 'advance.txt', 'advance\n')
        git(stack.writer, 'push', '-q', 'origin', 'main')
    monkeypatch.setattr(uc, '_m', lambda: fake_main(stack.live))
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: receipt.begin_update_receipt())
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace())
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    monkeypatch.setattr(uc, '_complete_source_update', lambda *a: pytest.fail('completion after failed preparation'))
    def fail(*a, **kw):
        data = json.loads(stack.p.journal_path(stack.live).read_text())
        assert data['phase'] == 'applied'
        assert data['head'] == git(stack.live, 'rev-parse', 'HEAD')
        raise RuntimeError('injected preparation failure')
    if point == 'request':
        monkeypatch.setattr(uc, '_source_completion_request', fail)
    elif point == 'fact':
        original_fact = receipt.record_fact
        def fact(name, value):
            if name == 'patch_stack':
                fail()
            return original_fact(name, value)
        monkeypatch.setattr(receipt, 'record_fact', fact)
    else:
        original_write = stack.p.write_journal
        def write(root, data):
            if data['phase'] == 'completion':
                fail()
            return original_write(root, data)
        monkeypatch.setattr(stack.p, 'write_journal', write)
    with receipt.update_receipt_scope():
        with pytest.raises(RuntimeError, match='injected preparation failure'):
            uc.dispatch_patch_stack(args())
    assert json.loads(stack.p.journal_path(stack.live).read_text())['phase'] == 'applied'
    result = receipt.read_latest_receipt()
    assert result is not None and result['outcome'] == 'partial'


@pytest.mark.parametrize('advance', [False, True])
def test_checkpoint_owner_death_retains_continuous_barrier(stack, advance):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    source = Path(__file__).resolve().parents[2]
    if advance:
        commit(stack.writer, 'advance.txt', 'advance\n')
        git(stack.writer, 'push', '-q', 'origin', 'main')
    script = f"""
import os
from pathlib import Path
from types import SimpleNamespace
from hermes_cli import update_cmd as uc, update_receipt as receipt
root = Path({str(stack.live)!r})
uc._m = lambda: SimpleNamespace(PROJECT_ROOT=root, _run_pre_update_backup=lambda a: None,
    _desktop_packaged_executable=lambda p: None, _desktop_dist_exists=lambda p: False,
    _installed_desktop_apps=lambda: [])
uc._resolve_update_options = lambda *a: SimpleNamespace()
uc._begin_update_receipt_and_plan = lambda a: receipt.begin_update_receipt()
uc._source_completion_request = lambda *a: {{'windows_resume': None}}
receipt.checkpoint_update_receipt = lambda: os._exit(86)
uc._complete_source_update = lambda request: os._exit(99)
uc.dispatch_patch_stack(SimpleNamespace(yes=True))
"""
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=source,
                            env=dict(os.environ), capture_output=True, text=True, timeout=60)
    assert result.returncode == 86, result.stdout + result.stderr
    head = git(stack.live, 'rev-parse', 'HEAD')
    assert (head != stack.head) == advance
    journal = stack.p.journal_path(stack.live)
    assert journal.exists(), 'published checkout has no pending barrier at receipt checkpoint'
    data = json.loads(journal.read_text())
    assert data['phase'] in ('applied', 'completion')
    assert data['head'] == head
    # Cold other-home ordinary launch must not enter legacy repair/activation.
    (stack.live / 'hermes_bootstrap.py').write_bytes((source / 'hermes_bootstrap.py').read_bytes())
    launch = f"""
import sys, importlib.util
from hermes_cli import venv_sync, _early_recovery
from pm import environments
import pm
pm.venv_is_current = lambda **kw: False
def forbidden(*a, **kw):
    raise AssertionError('ordinary launch crossed pending barrier')
venv_sync._finish_source_update = forbidden
_early_recovery.recover_if_needed = forbidden
_early_recovery.restore_interrupted_pull = forbidden
environments.activate_dependencies = forbidden
sys.argv = ['hermes', 'gateway', 'run']
spec = importlib.util.spec_from_file_location('hermes_bootstrap', {str(stack.live / 'hermes_bootstrap.py')!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""
    other = subprocess.run([sys.executable, '-B', '-c', launch], cwd=source,
                           env=dict(os.environ, HERMES_HOME=str(stack.live.parent / 'other-home')),
                           capture_output=True, text=True, timeout=60)
    assert other.returncode != 0
    assert 'pending transaction' in other.stdout + other.stderr
    assert 'ordinary launch crossed' not in other.stdout + other.stderr
    assert json.loads(journal.read_text()) == data


def test_completion_reservation_survives_owner_process_death(stack):
    import os
    import subprocess
    import sys
    from pathlib import Path
    source = Path(__file__).resolve().parents[2]
    script = f"""
import os
from pathlib import Path
from types import SimpleNamespace
from hermes_cli import update_cmd as uc, update_receipt as receipt
root = Path({str(stack.live)!r})
uc._m = lambda: SimpleNamespace(PROJECT_ROOT=root, _run_pre_update_backup=lambda a: None,
    _desktop_packaged_executable=lambda p: None, _desktop_dist_exists=lambda p: False,
    _installed_desktop_apps=lambda: [])
uc._resolve_update_options = lambda *a: SimpleNamespace()
uc._begin_update_receipt_and_plan = lambda a: receipt.begin_update_receipt()
uc._source_completion_request = lambda *a: {{'windows_resume': None}}
uc._complete_source_update = lambda request: os._exit(87)
uc.dispatch_patch_stack(SimpleNamespace(yes=True))
"""
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=source,
                            env=dict(os.environ), capture_output=True, text=True, timeout=60)
    assert result.returncode == 87, result.stdout + result.stderr
    import json
    journal = json.loads(stack.p.journal_path(stack.live).read_text())
    assert journal['phase'] == 'completion'
    assert journal['head'] == stack.head
    # Dead owner's OS lock is released; its durable lifecycle barrier is not.
    with stack.p.repository_lock(stack.live):
        with pytest.raises(stack.p.PatchStackError, match='pending transaction'):
            stack.p.require_no_pending(stack.live)
    from hermes_cli import update_receipt as receipt
    published = receipt.read_latest_receipt()
    assert published is not None and published['code_published'] is True


def test_fresh_receipt_boundary_classifies_published_failure_partial():
    from hermes_cli import update_receipt as receipt
    with receipt.update_receipt_scope():
        receipt.begin_update_receipt(previous={'code_published': True})
        path = receipt.finalize_update_receipt('failed')
    import json
    assert json.loads(path.read_text())['outcome'] == 'partial'

def test_impl_noop_bypasses_legacy_mutation_and_holds_repository_lock(stack, monkeypatch):
    from hermes_cli import update_cmd as uc
    from contextlib import contextmanager
    held = []
    original_lock = stack.p.repository_lock
    @contextmanager
    def lock(root):
        with original_lock(root):
            held.append(True)
            try:
                yield
            finally:
                held.pop()
    monkeypatch.setattr(stack.p, 'repository_lock', lock)
    stage, apply = stack.p.stage, stack.p.apply
    def guarded_stage(*a):
        assert held
        return stage(*a)
    def guarded_apply(*a):
        assert held
        return apply(*a)
    monkeypatch.setattr(stack.p, 'stage', guarded_stage)
    monkeypatch.setattr(stack.p, 'apply', guarded_apply)
    monkeypatch.setattr(uc, '_m', lambda: fake_main(stack.live))
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: None)
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace())
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    monkeypatch.setattr(uc, 'git_operation_in_progress', lambda *a: pytest.fail('legacy preflight'))
    calls = []
    def complete(request):
        assert held, 'repository lock released before completion'
        calls.append(request)
    monkeypatch.setattr(uc, '_complete_source_update', complete)
    uc._cmd_update_impl(args(), False)
    assert calls[0]['expected_sha'] == stack.head
    assert not held

def test_bundled_policy_refuses_before_options(stack, monkeypatch):
    from hermes_cli import update_cmd as uc, update_channel
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    monkeypatch.setattr(update_channel, '_read_stamp', lambda root: {'payload': 'bundled'})
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('packaged apply'))
    with pytest.raises(SystemExit) as error:
        uc.dispatch_patch_stack(args())
    assert error.value.code == 2


@pytest.mark.parametrize('failure', ['conflict', 'invalid-candidate', 'fetch'])
def test_prepublication_failures_skip_completion_and_keep_live_code(stack, monkeypatch, failure):
    from hermes_cli import update_cmd as uc, update_receipt as receipt
    monkeypatch.setattr(uc, '_m', lambda: fake_main(stack.live))
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: receipt.begin_update_receipt())
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace())
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    monkeypatch.setattr(uc, '_complete_source_update', lambda *a: pytest.fail('completion before publish'))
    if failure == 'conflict':
        commit(stack.writer, 'vault.txt', 'conflicting\n')
        git(stack.writer, 'push', '-q', 'origin', 'main')
    elif failure == 'invalid-candidate':
        commit(stack.writer, 'hermes_cli/main.py', 'syntax error !\n')
        git(stack.writer, 'push', '-q', 'origin', 'main')
    else:
        stack.upstream.rename(stack.upstream.with_name('offline.git'))
    with receipt.update_receipt_scope():
        with pytest.raises(SystemExit) as error:
            uc.dispatch_patch_stack(args())
    assert error.value.code == 1
    assert git(stack.live, 'rev-parse', 'HEAD') == stack.head
    assert git(stack.live, 'rev-parse', stack.p.base_ref(stack.live)) == stack.base
    assert receipt.read_latest_receipt()['outcome'] == 'failed'

def test_preflight_only_policy_leaves_apply_to_locked_command(stack, monkeypatch):
    from hermes_cli import update_cmd as uc
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('preflight attempted update'))
    assert uc.dispatch_patch_stack(args(), preflight_only=True) is False

@pytest.mark.parametrize('failure', [ValueError, OSError, KeyboardInterrupt])
@pytest.mark.parametrize('point', ['files', 'fsync', 'lost-git'])
def test_partial_filesystem_publication_receipt_is_partial(stack, monkeypatch, failure, point):
    from hermes_cli import update_cmd as uc, update_receipt as receipt
    monkeypatch.setattr(uc, '_m', lambda: fake_main(stack.live))
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: receipt.begin_update_receipt())
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace())
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    monkeypatch.setattr(uc, '_complete_source_update', lambda *a: pytest.fail('completion after failed publication'))
    commit(stack.writer, 'advance.txt', 'advance\n')
    git(stack.writer, 'push', '-q', 'origin', 'main')
    original_git = stack.p.git
    journal_file = stack.p.journal_path(stack.live)
    lost_git = False
    def inject(*a, **kw):
        nonlocal lost_git
        lost_git = point == 'lost-git'
        error_type = stack.p.PatchStackError if failure is ValueError else failure
        raise error_type('injected filesystem publication failure')
    def fail_files(root, *argv, **kwargs):
        if lost_git:
            raise OSError('cannot spawn Git after publication')
        if argv[0] == 'apply' and '--check' not in argv and point != 'fsync':
            inject()
        return original_git(root, *argv, **kwargs)
    monkeypatch.setattr(stack.p, 'git', fail_files)
    if point == 'fsync':
        monkeypatch.setattr(stack.p, 'sync_publication', inject)
    with receipt.update_receipt_scope():
        with pytest.raises((SystemExit, OSError, KeyboardInterrupt)):
            uc.dispatch_patch_stack(args())
    result = receipt.read_latest_receipt()
    assert result['outcome'] == 'partial'
    assert result['code_published'] is True
    assert result['patch_stack']['head'] == git(stack.live, 'rev-parse', 'HEAD')
    assert journal_file.exists()

def test_base_publication_with_unchanged_head_is_still_partial(stack, monkeypatch):
    from hermes_cli import update_cmd as uc, update_receipt as receipt
    git(stack.writer, 'fetch', '-q', str(stack.live), 'lucination')
    git(stack.writer, 'merge', '--ff-only', 'FETCH_HEAD')
    git(stack.writer, 'push', '-q', 'origin', 'main')
    monkeypatch.setattr(uc, '_m', lambda: fake_main(stack.live))
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: receipt.begin_update_receipt())
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace())
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    monkeypatch.setattr(uc, '_complete_source_update', lambda *a: pytest.fail('completion after failed fsync'))
    def failed(*a):
        raise OSError('post-publication fsync')
    monkeypatch.setattr(stack.p, 'sync_publication', failed)
    with receipt.update_receipt_scope():
        with pytest.raises(OSError, match='fsync'):
            uc.dispatch_patch_stack(args())
    result = receipt.read_latest_receipt()
    assert result and result['outcome'] == 'partial'
    assert result['code_published'] is True
    assert git(stack.live, 'rev-parse', 'HEAD') == stack.head
    assert stack.p.sha(stack.live, stack.p.base_ref(stack.live)) == stack.head


@pytest.mark.parametrize('preflight', [False, True])
@pytest.mark.parametrize('mode', [None, 'check', 'plan'])
def test_policy_absent_pending_setup_refuses_legacy_yes(stack, monkeypatch, preflight, mode):
    from hermes_cli import update_cmd as uc
    stack.p.clear(stack.live)
    journal = stack.p.journal_path(stack.live)
    journal.write_text('{"phase": "configure"}')
    monkeypatch.setattr(uc, '_m', lambda: fake_main(stack.live))
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('legacy options'))
    with pytest.raises(SystemExit) as error:
        uc.dispatch_patch_stack(args(yes=True, **({mode: True} if mode else {})), preflight_only=preflight)
    assert error.value.code == 2
    assert journal.read_text() == '{"phase": "configure"}'


def test_actual_interrupted_configure_bootstrap_and_main_retry_refuses(stack, monkeypatch):
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    from hermes_cli.config import get_config_path, load_config
    source = Path(__file__).resolve().parents[2]
    stack.p.clear(stack.live)
    def interrupted(*a):
        raise OSError('setup interrupted before policy write')
    with monkeypatch.context() as scoped:
        scoped.setattr(stack.p, '_write_policy', interrupted)
        with pytest.raises(OSError, match='setup interrupted'):
            stack.p.configure(stack.live, 'lucination', 'upstream/main')
    assert stack.p.read_policy(load_config(), stack.live) is None
    journal = stack.p.journal_path(stack.live)
    journal_before = journal.read_bytes()
    refs_before = git(stack.live, 'show-ref')
    shutil.copyfile(source / 'hermes_bootstrap.py', stack.live / 'hermes_bootstrap.py')
    shutil.copyfile(source / 'hermes_cli/main.py', stack.live / 'hermes_cli/main.py')
    config_before = get_config_path().read_bytes()
    script = f"""
import sys, importlib.util
from hermes_cli import venv_sync, _early_recovery
from pm import environments
def forbidden(*a, **kw):
    raise AssertionError('pending setup reached legacy repair')
venv_sync._finish_source_update = forbidden
_early_recovery.recover_if_needed = forbidden
_early_recovery.restore_interrupted_pull = forbidden
environments.activate_dependencies = forbidden
sys.argv = ['hermes', 'update', '--yes']
for name, path in [('hermes_bootstrap', {str(stack.live / 'hermes_bootstrap.py')!r}),
                   ('hermes_cli.main', {str(stack.live / 'hermes_cli/main.py')!r})]:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
module.PROJECT_ROOT = __import__('pathlib').Path({str(stack.live)!r})
module._run_pre_update_backup = forbidden
from hermes_cli import update_cmd, update_owning_install
update_cmd._m = lambda: module
update_owning_install.retarget_to_owning_install = lambda root: None
module.main()
"""
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=source,
                            env=dict(os.environ), capture_output=True, text=True, timeout=60)
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'pending transaction' in result.stdout + result.stderr
    assert journal.read_bytes() == journal_before
    assert get_config_path().read_bytes() == config_before
    assert git(stack.live, 'show-ref') == refs_before


def test_direct_check_uses_readonly_patch_stack_preview(stack, monkeypatch):
    from hermes_cli import update_cmd as uc
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    monkeypatch.setattr(uc._check, 'clear_git_debris', lambda *a: pytest.fail('legacy mutating check'))
    uc._cmd_update_check()


@pytest.mark.parametrize('branch,base', [('bad..branch', 'upstream/main'), ('lucination', 'upstream/dev')])
def test_invalid_setup_is_refused_without_movement(stack, monkeypatch, branch, base):
    from hermes_cli import update_cmd as uc
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    with pytest.raises(SystemExit) as error:
        uc.dispatch_patch_stack(args(set_patch_stack=branch, base=base))
    assert error.value.code == 2
    assert git(stack.live, 'rev-parse', 'HEAD') == stack.head


def test_existing_desktop_product_is_carried_to_completion(stack, monkeypatch):
    from hermes_cli import update_cmd as uc
    main = fake_main(stack.live)
    main._installed_desktop_apps = lambda: ['fixture-app']
    monkeypatch.setattr(uc, '_m', lambda: main)
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: None)
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace())
    def request(opts, plan, snapshot, windows, desktop, gateway):
        assert desktop is True
        return {'windows_resume': None}
    monkeypatch.setattr(uc, '_source_completion_request', request)
    monkeypatch.setattr(uc, '_complete_source_update', lambda *a: None)
    assert uc.dispatch_patch_stack(args())


def test_no_policy_no_pending_preserves_legacy_without_git_probe(tmp_path, monkeypatch):
    from hermes_cli import update_cmd as uc, config, update_patch_stack as stack_module
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=tmp_path))
    monkeypatch.setattr(config, 'require_readable_config_before_write', lambda: {})
    monkeypatch.setattr(stack_module, 'git', lambda *a, **kw: pytest.fail('legacy admission probed Git'))
    assert uc.dispatch_patch_stack(args(yes=True), preflight_only=True) is False


def test_malformed_policy_refuses_without_legacy_fallback(stack, monkeypatch):
    from hermes_cli import update_cmd as uc, config
    monkeypatch.setattr(uc, '_m', lambda: SimpleNamespace(PROJECT_ROOT=stack.live))
    monkeypatch.setattr(config, 'require_readable_config_before_write', lambda: {'update': {'installs': 'invalid'}})
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('malformed policy applied'))
    with pytest.raises(SystemExit) as error:
        uc.dispatch_patch_stack(args())
    assert error.value.code == 2


@pytest.fixture
def real_main_boundary(stack, monkeypatch):
    """Exercise the actual cmd_update boundary, not a fake main namespace."""
    from hermes_cli import main, update_cmd as uc
    monkeypatch.setattr(main, 'PROJECT_ROOT', stack.live)
    monkeypatch.setattr(uc, '_m', lambda: main)
    monkeypatch.setattr('hermes_cli.update_owning_install.retarget_to_owning_install', lambda root: None)
    monkeypatch.setattr(main, '_install_hangup_protection', lambda **kw: None)
    monkeypatch.setattr(main, '_finalize_update_output', lambda state: None)
    parser, subparsers = main._build_cli_parser()
    return main, uc, lambda argv: main._parse_cli_args(parser, subparsers, ['update', *argv])


@pytest.mark.parametrize('argv', [
    ['--set-patch-stack', 'lucination', '--base', 'upstream/main'],
    ['--clear-patch-stack'],
])
def test_main_configure_clear_bypass_legacy_metadata_and_hold_repository_lock(stack, real_main_boundary, monkeypatch, argv):
    main, uc, parse = real_main_boundary
    monkeypatch.setattr('hermes_cli.update_channel.handle_metadata_args', lambda *a: pytest.fail('legacy metadata before configure/clear'))
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: pytest.fail('configure/clear applied code'))
    seen = []
    write = stack.p._write_policy
    def guarded_write(root, policy):
        key = (__import__('os').getpid(), str(stack.p.common_directory(root) / 'hermes-patch-stacks'))
        assert key in stack.p._locks.held
        seen.append(policy)
        return write(root, policy)
    monkeypatch.setattr(stack.p, '_write_policy', guarded_write)
    main.cmd_update(parse(argv))
    assert len(seen) == 1
    assert git(stack.live, 'rev-parse', 'HEAD') == stack.head
    from hermes_cli.config import load_config
    policy = stack.p.read_policy(load_config(), stack.live)
    assert (policy is None) == (argv == ['--clear-patch-stack'])


@pytest.mark.parametrize('argv', [
    ['--branch', 'main'], ['--channel', 'main'], ['--set-channel', 'main'],
    ['--switch-branch'], ['--keep-stash'],
])
def test_main_policy_override_refusal_precedes_legacy_metadata(stack, real_main_boundary, monkeypatch, argv):
    main, uc, parse = real_main_boundary
    monkeypatch.setattr('hermes_cli.update_channel.handle_metadata_args', lambda *a: pytest.fail('legacy metadata before policy refusal'))
    monkeypatch.setattr(main, '_install_hangup_protection', lambda **kw: pytest.fail('refusal acquired update IO'))
    with pytest.raises(SystemExit) as error:
        main.cmd_update(parse(argv))
    assert error.value.code == 2
    assert git(stack.live, 'rev-parse', 'HEAD') == stack.head


def test_main_ordinary_apply_holds_command_lock_and_repository_lock(stack, real_main_boundary, monkeypatch):
    main, uc, parse = real_main_boundary
    from hermes_cli import update_lock
    held = []
    original = update_lock.UpdateLock
    class TrackedLock(original):
        def acquire(self):
            result = super().acquire()
            if result:
                held.append(True)
            return result
        def release(self):
            super().release()
            held.pop()
    monkeypatch.setattr(update_lock, 'UpdateLock', TrackedLock)
    commit(stack.writer, 'advance.txt', 'advance\n')
    git(stack.writer, 'push', '-q', 'origin', 'main')
    monkeypatch.setattr(main, '_run_pre_update_backup', lambda a: None)
    monkeypatch.setattr(main, '_installed_desktop_apps', lambda: [])
    monkeypatch.setattr(uc, '_begin_update_receipt_and_plan', lambda a: None)
    monkeypatch.setattr(uc, '_resolve_update_options', lambda *a: SimpleNamespace())
    monkeypatch.setattr(uc, '_source_completion_request', lambda *a: {'windows_resume': None})
    calls = []
    stage, apply = stack.p.stage, stack.p.apply
    def guarded(function):
        def call(root, *a):
            assert held
            key = (__import__('os').getpid(), str(stack.p.common_directory(root) / 'hermes-patch-stacks'))
            assert key in stack.p._locks.held
            calls.append(function.__name__)
            return function(root, *a)
        return call
    monkeypatch.setattr(stack.p, 'stage', guarded(stage))
    monkeypatch.setattr(stack.p, 'apply', guarded(apply))
    def complete(request):
        assert held
        import os, subprocess, sys
        script = f"from pathlib import Path\nfrom hermes_cli.update_patch_stack import repository_lock\nwith repository_lock(Path({str(stack.live)!r})):\n print('unexpected acquisition')"
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                env=dict(os.environ, HERMES_HOME=str(stack.live / 'other-home')),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert 'repository locked' in result.stderr
        assert request['expected_sha'] == git(stack.live, 'rev-parse', 'HEAD')
        calls.append('completion')
    monkeypatch.setattr(uc, '_complete_source_update', complete)
    monkeypatch.setattr(uc, 'git_operation_in_progress', lambda *a: pytest.fail('legacy apply mutation'))
    main.cmd_update(parse(['--yes', '--no-backup', '--no-gateway-restart']))
    assert calls == ['stage', 'apply', 'completion']
    assert not held
