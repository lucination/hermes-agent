"""Publication races and durable interruption barriers with real Git."""
import json
import os
import subprocess
import sys
import pytest
from tests.hermes_cli.test_update_patch_stack_git import stack, git, commit


def test_git_sanitizes_hostile_environment_and_replace_refs(stack, monkeypatch):
    t = stack
    git(t.live, 'replace', t.head, t.base)
    monkeypatch.setenv('GIT_DIR', str(t.upstream))
    monkeypatch.setenv('GIT_WORK_TREE', str(t.writer))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'alias.bad')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', '!false')
    assert t.p.sha(t.live, 'HEAD') == t.head
    assert t.p.git(t.live, 'show', 'HEAD:vault.txt').stdout == 'vault patch\n'
    assert t.p.git(t.live, 'config', '--get', 'alias.bad', check=False).returncode != 0


@pytest.mark.parametrize('fault', ['missing', 'syntax'])
def test_invalid_startup_candidate_is_retained_without_publication(stack, fault):
    t = stack
    if fault == 'missing':
        git(t.writer, 'rm', 'run_agent.py')
        git(t.writer, 'commit', '-qm', 'missing startup')
    else:
        commit(t.writer, 'run_agent.py', 'def invalid(:\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    with pytest.raises(t.p.PatchStackError, match='startup'):
        t.p.stage(t.live, t.policy)
    assert git(t.live, 'rev-parse', 'HEAD') == t.head
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'invalid-candidate'


@pytest.mark.parametrize('name', [
    'hermes_cli/update_patch_stack.py', 'hermes_cli/update_channel.py',
    'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_check.py', 'hermes_bootstrap.py',
    'hermes_cli/update_completion.py', 'hermes_cli/source_completion.py',
    'hermes_cli/source_check.py', 'hermes_cli/venv_sync.py', 'hermes_cli/update_receipt.py',
])
@pytest.mark.parametrize('fault', ['missing', 'syntax', 'symlink'])
def test_invalid_carried_candidate_module_refuses_before_publication(stack, name, fault):
    t = stack
    if fault == 'missing':
        git(t.writer, 'rm', name)
        git(t.writer, 'commit', '-qm', 'missing carried module')
    elif fault == 'symlink':
        (t.writer / name).unlink()
        (t.writer / name).symlink_to('config.py' if name.startswith('hermes_cli/') else 'cli.py')
        git(t.writer, 'add', name)
        git(t.writer, 'commit', '-qm', 'symlink carried module')
    else:
        commit(t.writer, name, 'def invalid(:\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    with pytest.raises(t.p.PatchStackError, match='startup'):
        t.p.stage(t.live, t.policy)
    assert git(t.live, 'rev-parse', 'HEAD') == t.head
    assert git(t.live, 'rev-parse', t.p.base_ref(t.live)) == t.base
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'invalid-candidate'


def test_setup_interruption_retains_barrier_even_without_policy(stack, monkeypatch):
    t = stack
    t.p.clear(t.live)
    def fail(*args):
        raise OSError('interrupted config write')
    monkeypatch.setattr(t.p, '_write_policy', fail)
    with pytest.raises(OSError):
        t.p.configure(t.live, 'lucination', 'upstream/main')
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'configuring'
    with pytest.raises(t.p.PatchStackError, match='pending'):
        t.p.configure(t.live, 'lucination', 'upstream/main')


def test_persisted_policy_requires_explicit_anchor_and_rejects_malformed_tokens(stack):
    from hermes_cli.config import load_config
    from hermes_cli.update_channel import channel_record
    t = stack
    config = load_config()
    record = channel_record(config, t.live)
    assert record['patch_stack']['anchor_sha'] == t.base
    assert t.p.read_policy(config, t.live) == t.policy
    del record['patch_stack']['anchor_sha']
    with pytest.raises(t.p.PatchStackError):
        t.p.read_policy(config, t.live)


def test_channel_write_refuses_pending_setup_without_saved_policy(stack, monkeypatch):
    from hermes_cli.update_channel import set_install_channel
    t = stack
    t.p.clear(t.live)
    t.p.write_journal(t.live, {'phase': 'configuring'})
    with pytest.raises(t.p.PatchStackError, match='pending'):
        set_install_channel('stable', t.live)


@pytest.mark.parametrize('pending', [False, True])
def test_windows_unconfigured_channel_avoids_unsupported_repository_lock(stack, monkeypatch, pending):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from hermes_cli import update_channel as channel
    from hermes_cli.config import load_config
    t = stack
    t.p.clear(t.live)
    if pending:
        t.p.write_journal(t.live, {'phase': 'configuring'})
    def unsupported(root):
        raise t.p.PatchStackError('Repository locking unsupported; fail closed')
    monkeypatch.setattr(channel, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(channel, '_channel_write_lock', lambda path: nullcontext())
    monkeypatch.setattr(t.p, 'repository_lock', unsupported)
    if pending:
        with pytest.raises(t.p.PatchStackError, match='pending'):
            channel.set_install_channel('stable', t.live)
    else:
        channel.set_install_channel('stable', t.live)
        assert channel.channel_record(load_config(), t.live)['channel'] == 'stable'


def test_other_worktree_cannot_configure_while_candidate_is_pending(stack, tmp_path):
    t = stack
    advance(t)
    other = tmp_path / 'other'
    git(t.live, 'worktree', 'add', '--detach', str(other), t.head)
    with pytest.raises(t.p.PatchStackError, match='pending'):
        t.p.configure(other, 'lucination', 'upstream/main')


def test_unattended_stage_ignores_unsafe_rebase_settings_and_hooks(stack):
    t = stack
    marker = t.live.parent / 'hook-ran'
    hook = t.live / '.git/hooks/post-checkout'
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
    hook.chmod(0o755)
    for key, value in [('rebase.autoStash', 'true'), ('rebase.updateRefs', 'true'),
                       ('rebase.backend', 'apply'), ('commit.gpgsign', 'true'),
                       ('rerere.enabled', 'true')]:
        git(t.live, 'config', key, value)
    git(t.live, 'branch', 'must-not-move', t.head)
    candidate = advance(t)
    assert not marker.exists()
    assert git(t.live, 'rev-parse', 'must-not-move') == t.head
    assert git(t.live, 'stash', 'list') == ''
    assert candidate.head != t.head


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_hidden_index_edits_refuse_before_staging(stack, flag):
    t = stack
    git(t.live, 'update-index', flag, 'vault.txt')
    (t.live / 'vault.txt').write_text('hidden edit\n')
    with pytest.raises(t.p.PatchStackError, match='index'):
        t.p.stage(t.live, t.policy)
    assert (t.live / 'vault.txt').read_text() == 'hidden edit\n'


def test_custom_merge_driver_refuses_before_staging(stack):
    t = stack
    marker = t.live.parent / 'merge-driver-ran'
    git(t.live, 'config', 'merge.danger.driver', f'touch {marker}; true')
    commit(t.live, '.gitattributes', 'core.txt merge=danger\n')
    commit(t.live, 'core.txt', 'local conflict\n')
    before = git(t.live, 'rev-parse', 'HEAD')
    commit(t.writer, '.gitattributes', 'core.txt merge=danger\n')
    commit(t.writer, 'core.txt', 'upstream conflict\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    with pytest.raises(t.p.PatchStackError, match='merge driver'):
        t.p.stage(t.live, t.policy)
    assert not marker.exists()
    assert not t.p.journal_path(t.live).exists()
    assert git(t.live, 'rev-parse', 'HEAD') == before


def test_configured_external_filter_refuses_before_candidate_checkout(stack):
    t = stack
    marker = t.live.parent / 'filter-ran'
    git(t.live, 'config', 'filter.danger.smudge', f'touch {marker}; cat')
    commit(t.writer, '.gitattributes', 'advance.txt filter=danger\n')
    commit(t.writer, 'advance.txt', 'advance\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    with pytest.raises(t.p.PatchStackError, match='filter'):
        t.p.stage(t.live, t.policy)
    assert not marker.exists()
    assert git(t.live, 'rev-parse', 'HEAD') == t.head


def test_journal_syncs_parent_directory_before_ref_mutation(stack, monkeypatch):
    import stat
    t = stack
    synced = []
    fsync = os.fsync
    def record(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return fsync(fd)
    monkeypatch.setattr(os, 'fsync', record)
    t.p.write_journal(t.live, {'phase': 'test'})
    assert True in synced


@pytest.fixture
def fresh_stack_checkout(stack, tmp_path):
    t = stack
    root = tmp_path / 'fresh-install'
    git(tmp_path, 'clone', '-q', str(t.live), str(root))
    git(root, 'remote', 'add', 'upstream', str(t.upstream))
    git(root, 'fetch', '-q', 'upstream')
    assert not (t.p.common_directory(root) / 'hermes-patch-stacks').exists()
    return t.p, root


def test_fresh_journal_directory_parent_is_synced_before_first_journal_returns(
        fresh_stack_checkout, monkeypatch):
    from pathlib import Path
    p, root = fresh_stack_checkout
    common = p.common_directory(root)
    expected = common.stat()
    events = []
    original_open, original_sync, original_journal = os.open, os.fsync, p.write_journal
    def opening(name, flags, *args, **kwargs):
        fd = original_open(name, flags, *args, **kwargs)
        if Path(name) == common:
            events.append('parent-open')
        return fd
    def sync(fd):
        actual = os.fstat(fd)
        result = original_sync(fd)
        if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
            events.append('parent-fsync')
        return result
    def journal(root, data):
        result = original_journal(root, data)
        assert events[:2] == ['parent-open', 'parent-fsync'], events
        events.append('journal-return')
        return result
    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(p, 'write_journal', journal)
    p.configure(root, 'lucination', 'upstream/main')
    assert events.index('parent-fsync') < events.index('journal-return')


@pytest.mark.parametrize('operation', ['open', 'fsync'])
def test_journal_directory_parent_persistence_failure_prevents_transaction_writes(
        fresh_stack_checkout, monkeypatch, operation):
    from pathlib import Path
    from hermes_cli.config import get_config_path
    p, root = fresh_stack_checkout
    common = p.common_directory(root)
    expected = common.stat()
    before_refs = git(root, 'show-ref')
    before_head = git(root, 'symbolic-ref', 'HEAD')
    before_config = get_config_path().read_bytes()
    before_index = (common / 'index').read_bytes()
    original_open, original_sync, original_git = os.open, os.fsync, p.git
    def opening(name, flags, *args, **kwargs):
        if Path(name) == common:
            raise OSError('injected parent persistence failure')
        return original_open(name, flags, *args, **kwargs)
    def sync(fd):
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
            raise OSError('injected parent persistence failure')
        return original_sync(fd)
    def forbidden(*args, **kwargs):
        pytest.fail('unsafe transaction write before parent persistence')
    def command(root, *args, **kwargs):
        if args[0] == 'update-ref':
            forbidden()
        return original_git(root, *args, **kwargs)
    monkeypatch.setattr(os, operation, opening if operation == 'open' else sync)
    monkeypatch.setattr(p, 'write_journal', forbidden)
    monkeypatch.setattr(p, '_write_policy', forbidden)
    monkeypatch.setattr(p, 'git', command)
    with pytest.raises(p.PatchStackError, match='persistence failed'):
        p.configure(root, 'lucination', 'upstream/main')
    assert git(root, 'show-ref') == before_refs
    assert git(root, 'symbolic-ref', 'HEAD') == before_head
    assert get_config_path().read_bytes() == before_config
    assert (common / 'index').read_bytes() == before_index
    assert not list((common / 'hermes-patch-stacks').iterdir())


def test_stage_requires_current_explicit_install_policy(stack):
    t = stack
    t.p.clear(t.live)
    with pytest.raises(t.p.PatchStackError, match='policy'):
        t.p.stage(t.live, t.policy)


def test_live_home_guard_still_blocks_content_reads():
    from pathlib import Path
    from hermes_cli.config import get_config_path

    # HOME may be isolated by the canonical runner. Exercise its actual
    # default-home boundary, not an operator-specific credentials path.
    guarded_root = Path.home() / '.hermes'
    assert not get_config_path().is_relative_to(guarded_root)
    with pytest.raises(AssertionError, match='REAL hermes home'):
        # A nonexistent probe avoids reading credentials even if the guard
        # regresses; FileNotFoundError must not satisfy this assertion.
        (guarded_root / 'patch-stack-home-guard-probe-never-created').read_text()


@pytest.mark.parametrize('key,expected', [('core.fsync', 'all'), ('core.fsyncMethod', 'fsync')])
def test_git_enforces_durable_writes_despite_repository_config(stack, key, expected):
    t = stack
    git(t.live, 'config', key, 'none' if key == 'core.fsync' else 'writeout-only')
    assert t.p.git(t.live, 'config', '--get', key).stdout.strip() == expected


def test_git_disables_repository_ssh_commands_and_external_protocol(stack):
    t = stack
    git(t.live, 'config', 'core.sshCommand', 'touch /must-not-run')
    git(t.live, 'config', 'protocol.ext.allow', 'always')
    assert t.p.git(t.live, 'config', '--get', 'core.sshCommand').stdout.strip() == 'ssh -oBatchMode=yes'
    assert t.p.git(t.live, 'config', '--get', 'protocol.ext.allow').stdout.strip() == 'never'


def advance(t):
    commit(t.writer, 'advance.txt', 'advance\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    return t.p.stage(t.live, t.policy)


def test_common_directory_lock_refuses_other_worktree_and_home(stack, tmp_path):
    t = stack
    other = tmp_path / 'other'
    git(t.live, 'worktree', 'add', '--detach', str(other), t.head)
    with t.p.repository_lock(t.live):
        env = dict(os.environ, HERMES_HOME=str(tmp_path / 'different-home'))
        result = subprocess.run([sys.executable, '-c',
            'from pathlib import Path; from hermes_cli.update_patch_stack import repository_lock; '
            f'\nwith repository_lock(Path({str(other)!r})): pass'], env=env, text=True, capture_output=True)
        assert result.returncode != 0
        assert 'locked' in result.stderr
    with t.p.repository_lock(other):
        pass


@pytest.mark.parametrize('race', ['dirty', 'head', 'base', 'branch', 'candidate'])
def test_apply_refuses_changed_state_without_overwriting(stack, race):
    t = stack
    candidate = advance(t)
    if race == 'dirty':
        (t.live / 'vault.txt').write_text('new edit\n')
    elif race == 'head':
        commit(t.live, 'new.txt', 'new patch\n')
    elif race == 'base':
        git(t.live, 'update-ref', t.p.base_ref(t.live), t.first)
    elif race == 'branch':
        git(t.live, 'checkout', '-q', 'main')
    else:
        (candidate.path / 'vault.txt').write_text('candidate edit\n')
    before = git(t.live, 'rev-parse', 'HEAD'), git(t.live, 'status', '--porcelain'), (t.live / 'core.txt').read_bytes()
    with pytest.raises(t.p.PatchStackError):
        t.p.apply(t.live, t.policy, candidate)
    assert before == (git(t.live, 'rev-parse', 'HEAD'), git(t.live, 'status', '--porcelain'), (t.live / 'core.txt').read_bytes())


@pytest.mark.parametrize('timing', ['before', 'publishing-files', 'git-publication'])
def test_ignored_collision_preserves_local_bytes(stack, monkeypatch, timing):
    t = stack
    (t.live / '.git/info/exclude').write_text('private.bin\n')
    commit(t.writer, 'private.bin', 'candidate bytes\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    candidate = t.p.stage(t.live, t.policy)
    private = t.live / 'private.bin'
    original = b'local ignored bytes\x00\xff'
    journal, command = t.p.write_journal, t.p.git
    if timing == 'before':
        private.write_bytes(original)
    def write(root, data):
        result = journal(root, data)
        if timing == 'publishing-files' and data['phase'] == 'publishing-files':
            private.write_bytes(original)
        return result
    def run(root, *args, **kwargs):
        if (timing == 'git-publication' and root == t.live
                and args[0] in ('read-tree', 'apply', 'checkout') and '--check' not in args):
            private.write_bytes(original)
        return command(root, *args, **kwargs)
    monkeypatch.setattr(t.p, 'write_journal', write)
    monkeypatch.setattr(t.p, 'git', run)
    with pytest.raises(t.p.PatchStackError):
        t.p.apply(t.live, t.policy, candidate)
    assert private.read_bytes() == original
    assert t.p.journal_path(t.live).exists()
    if timing == 'before':
        assert git(t.live, 'rev-parse', 'HEAD') == t.head
        assert git(t.live, 'rev-parse', t.p.base_ref(t.live)) == t.base


@pytest.mark.parametrize('race', ['head', 'base', 'branch'])
def test_ref_race_during_publication_never_changes_files(stack, monkeypatch, race):
    t = stack
    candidate = advance(t)
    original = t.p.write_journal
    def journal(root, data):
        result = original(root, data)
        if data['phase'] == 'publishing-files':
            if race == 'head':
                git(t.live, 'update-ref', 'refs/heads/lucination', t.head)
            elif race == 'base':
                git(t.live, 'update-ref', t.p.base_ref(t.live), t.base)
            else:
                git(t.live, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        return result
    monkeypatch.setattr(t.p, 'write_journal', journal)
    with pytest.raises(t.p.PatchStackError):
        t.p.apply(t.live, t.policy, candidate)
    assert not (t.live / 'advance.txt').exists()
    assert (t.live / 'vault.txt').read_text() == 'vault patch\n'
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'publishing-files'


def test_detachment_after_file_publication_retains_recovery_barrier(stack, monkeypatch):
    t = stack
    candidate = advance(t)
    original = t.p.git
    def command(root, *args, **kwargs):
        result = original(root, *args, **kwargs)
        if root == t.live and args[0] in ('read-tree', 'apply') and '--check' not in args:
            git(t.live, 'update-ref', '--no-deref', 'HEAD', candidate.head)
        return result
    monkeypatch.setattr(t.p, 'git', command)
    with pytest.raises(t.p.PatchStackError):
        t.p.apply(t.live, t.policy, candidate)
    assert t.p.journal_path(t.live).exists()


def test_lock_backend_failure_refuses_without_stage_effects(stack, monkeypatch):
    import fcntl
    t = stack
    def fail(*args):
        raise OSError('lock backend unavailable')
    monkeypatch.setattr(fcntl, 'flock', fail)
    with pytest.raises(t.p.PatchStackError, match='locking unavailable'):
        t.p.stage(t.live, t.policy)
    assert not t.p.journal_path(t.live).exists()
    assert git(t.live, 'rev-parse', 'HEAD') == t.head


def test_missing_lock_backend_refuses_without_stage_effects(stack, monkeypatch):
    t = stack
    monkeypatch.setitem(sys.modules, 'fcntl', None)
    with pytest.raises(t.p.PatchStackError, match='locking unavailable'):
        t.p.stage(t.live, t.policy)
    assert not t.p.journal_path(t.live).exists()


def test_non_posix_locking_fails_closed(stack, monkeypatch):
    from types import SimpleNamespace
    t = stack
    common = t.p.common_directory(t.live)
    monkeypatch.setattr(t.p, 'common_directory', lambda root: common)
    monkeypatch.setattr(t.p, 'os', SimpleNamespace(name='nt', getpid=os.getpid))
    with pytest.raises(t.p.PatchStackError, match='locking unsupported'):
        with t.p.repository_lock(t.live):
            pytest.fail('unsupported platform acquired repository lock')
    assert not list((common / 'hermes-patch-stacks').glob('*.json'))


def test_durable_candidate_and_recovery_refs(stack):
    t = stack
    candidate = advance(t)
    common = t.p.common_directory(t.live)
    assert candidate.path.is_relative_to(common / 'hermes-patch-stacks')
    journal = json.loads(t.p.journal_path(t.live).read_text())
    assert git(t.live, 'rev-parse', journal['recovery_head']) == t.head
    assert git(t.live, 'rev-parse', journal['recovery_base']) == t.base


@pytest.mark.parametrize('phase', ['publishing-files', 'apply', 'applied'])
def test_interrupted_publication_refuses_reentry_with_both_refs_preserved(stack, monkeypatch, phase):
    t = stack
    candidate = advance(t)
    original_journal, original_git = t.p.write_journal, t.p.git
    def journal(root, data):
        if data['phase'] == phase:
            raise OSError('injected crash')
        return original_journal(root, data)
    def command(root, *args, **kwargs):
        if args[0] == phase and '--check' not in args:
            raise t.p.PatchStackError('injected crash')
        return original_git(root, *args, **kwargs)
    monkeypatch.setattr(t.p, 'write_journal', journal)
    monkeypatch.setattr(t.p, 'git', command)
    with pytest.raises((OSError, t.p.PatchStackError)):
        t.p.apply(t.live, t.policy, candidate)
    assert git(t.live, 'rev-parse', 'HEAD') == candidate.head
    assert git(t.live, 'rev-parse', t.p.base_ref(t.live)) == candidate.target
    data = json.loads(t.p.journal_path(t.live).read_text())
    assert data['phase'] in ('publishing-refs', 'publishing-files')
    assert git(t.live, 'rev-parse', data['recovery_head']) == t.head
    assert git(t.live, 'rev-parse', data['recovery_base']) == t.base
    with pytest.raises(t.p.PatchStackError):
        t.p.apply(t.live, t.policy, candidate)
    with pytest.raises(t.p.PatchStackError, match='pending'):
        t.p.clear(t.live)


def test_publication_persists_git_index_files_and_directories_before_journal_removal(stack, monkeypatch):
    t = stack
    candidate = advance(t)
    synced = set()
    original_sync, original_finish = os.fsync, t.p.finish_journal
    def identity(path):
        stat = path.stat()
        return stat.st_dev, stat.st_ino
    def sync(fd):
        stat = os.fstat(fd)
        synced.add((stat.st_dev, stat.st_ino))
        return original_sync(fd)
    def finish(root):
        common = t.p.common_directory(root)
        targets = [root, root / 'advance.txt', root / '.git/index',
                   common, common / 'refs', common / 'refs/heads',
                   common / 'refs/heads/lucination', common / t.p.base_ref(root),
                   common / 'objects', common / 'objects' / candidate.head[:2],
                   common / 'objects' / candidate.head[:2] / candidate.head[2:]]
        missing = [str(path) for path in targets if identity(path) not in synced]
        assert not missing, f'Journal removed before persistence: {missing}'
        return original_finish(root)
    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(t.p, 'finish_journal', finish)
    t.p.apply(t.live, t.policy, candidate)
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'applied'
    # Simulate the orchestrator discharging completion after all publication
    # persistence assertions; apply itself must never remove the barrier.
    t.p.finish_journal(t.live)
    assert not t.p.journal_path(t.live).exists()


@pytest.mark.parametrize('target', ['index', 'tracked-file', 'directory'])
@pytest.mark.parametrize('operation', ['open', 'fsync'])
def test_os_persistence_failure_retains_published_transaction(stack, monkeypatch, target, operation):
    from pathlib import Path
    t = stack
    candidate = advance(t)
    path = {'index': t.live / '.git/index', 'tracked-file': t.live / 'advance.txt',
            'directory': t.live}[target]
    original_sync, original_open = os.fsync, os.open
    def sync(fd):
        actual = os.fstat(fd)
        expected = path.stat() if path.exists() else None
        if expected and (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
            raise OSError('injected persistence failure')
        return original_sync(fd)
    def opening(name, flags, *args, **kwargs):
        if Path(name) == path:
            raise OSError('injected persistence failure')
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, operation, sync if operation == 'fsync' else opening)
    with pytest.raises(t.p.PatchStackError, match='persistence failed'):
        t.p.apply(t.live, t.policy, candidate)
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'applied'
    assert git(t.live, 'rev-parse', 'HEAD') == candidate.head
    with pytest.raises(t.p.PatchStackError, match='pending'):
        t.p.clear(t.live)


@pytest.mark.parametrize('after_unlink', [False, True])
def test_journal_removal_sync_failure_restores_pending_barrier(stack, monkeypatch, after_unlink):
    t = stack
    t.p.write_journal(t.live, {'phase': 'applied'})
    original = t.p.sync_directory
    def fail(path):
        if path == t.p.journal_path(t.live).parent and (
                not after_unlink or not t.p.journal_path(t.live).exists()):
            raise OSError('injected removal sync failure')
        return original(path)
    monkeypatch.setattr(t.p, 'sync_directory', fail)
    with pytest.raises(t.p.PatchStackError, match='journal'):
        t.p.finish_journal(t.live)
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'applied'


def test_ref_transaction_failure_retains_journal_and_never_changes_files(stack, monkeypatch):
    t = stack
    candidate = advance(t)
    original = t.p.git
    def failing(root, *args, **kwargs):
        if args == ('update-ref', '--stdin'):
            raise t.p.PatchStackError('injected transaction failure')
        return original(root, *args, **kwargs)
    monkeypatch.setattr(t.p, 'git', failing)
    with pytest.raises(t.p.PatchStackError):
        t.p.apply(t.live, t.policy, candidate)
    assert git(t.live, 'rev-parse', 'HEAD') == t.head
    assert git(t.live, 'rev-parse', t.p.base_ref(t.live)) == t.base
    assert not (t.live / 'advance.txt').exists()
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'publishing-refs'
    with pytest.raises(t.p.PatchStackError, match='pending'):
        t.p.stage(t.live, t.policy)
