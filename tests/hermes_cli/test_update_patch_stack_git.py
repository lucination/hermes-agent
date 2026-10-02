"""Real local bare upstream/fork fixtures; no network or live checkout."""
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest


def git(root, *args, check=True):
    result = subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                             '-c', 'commit.gpgsign=false', *args], cwd=root, text=True,
                            capture_output=True, check=check)
    return result.stdout.strip()


def commit(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    git(root, 'add', name)
    git(root, 'commit', '-qm', name)
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture
def stack(tmp_path):
    p = importlib.import_module('hermes_cli.update_patch_stack')
    upstream = tmp_path / 'upstream.git'
    git(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(upstream))
    writer = tmp_path / 'writer'
    git(tmp_path, 'clone', '-q', str(upstream), str(writer))
    for name in ('hermes_cli/main.py', 'hermes_cli/config.py', 'hermes_cli/__init__.py',
                 'hermes_cli/web_server.py', 'cli.py', 'run_agent.py', 'model_tools.py',
                 'toolsets.py', 'hermes_constants.py', 'hermes_bootstrap.py',
                 'hermes_cli/update_patch_stack.py', 'hermes_cli/update_channel.py',
                 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_check.py',
                 'hermes_cli/update_completion.py', 'hermes_cli/source_completion.py',
                 'hermes_cli/source_check.py', 'hermes_cli/venv_sync.py',
                 'hermes_cli/update_receipt.py'):
        path = writer / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# startup fixture\n')
    git(writer, 'add', '.')
    base = commit(writer, 'core.txt', 'base\n')
    git(writer, 'push', '-q', 'origin', 'main')
    fork = tmp_path / 'fork.git'
    git(tmp_path, 'clone', '-q', '--bare', str(upstream), str(fork))
    live = tmp_path / 'install'
    git(tmp_path, 'clone', '-q', str(fork), str(live))
    git(live, 'config', 'user.name', 'Fixture')
    git(live, 'config', 'user.email', 'fixture@example.invalid')
    git(live, 'remote', 'add', 'upstream', str(upstream))
    git(live, 'fetch', '-q', 'upstream')
    git(live, 'checkout', '-qb', 'lucination')
    first = commit(live, 'vault.txt', 'vault patch\n')
    head = commit(live, 'otp.txt', 'otp patch\n')
    policy = p.configure(live, 'lucination', 'upstream/main')
    return SimpleNamespace(p=p, live=live, writer=writer, upstream=upstream, fork=fork,
                           policy=policy, base=base, head=head, first=first)


def test_observation_preserves_index_bytes_after_tracked_mtime_change(stack):
    t = stack
    index = t.live / '.git/index'
    before = index.read_bytes()
    tracked = t.live / 'vault.txt'
    stamp = tracked.stat()
    os.utime(tracked, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
    assert not t.p.observe(t.live, t.policy).hazards
    assert index.read_bytes() == before


def test_detached_staging_then_guarded_publish_preserves_stack(stack):
    t = stack
    target = commit(t.writer, 'advance.txt', 'upstream advance\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    candidate = t.p.stage(t.live, t.policy)
    assert git(t.live, 'rev-parse', 'HEAD') == t.head
    assert git(t.live, 'rev-parse', t.p.base_ref(t.live)) == t.base
    assert git(candidate.path, 'branch', '--show-current') == ''
    assert candidate.target == target
    applied = t.p.apply(t.live, t.policy, candidate)
    assert applied.head == git(t.live, 'rev-parse', 'HEAD')
    assert git(t.live, 'branch', '--show-current') == 'lucination'
    assert git(t.live, 'rev-parse', t.p.base_ref(t.live)) == target
    assert git(t.live, 'rev-list', '--count', f'{target}..HEAD') == '2'
    assert git(t.live, 'rev-list', '--merges', f'{target}..HEAD') == ''
    assert (t.live / 'vault.txt').read_text(encoding='utf-8') == 'vault patch\n'
    with pytest.raises(t.p.PatchStackError, match='pending'):
        t.p.stage(t.live, t.policy)
    import json
    assert json.loads(t.p.journal_path(t.live).read_text())['phase'] == 'applied'
    # Manual apply callers must complete the tail before discharging its barrier.
    t.p.finish_journal(t.live)
    assert t.p.stage(t.live, t.policy).head == applied.head
    assert git(t.fork, 'rev-parse', 'main') == t.base


@pytest.mark.parametrize('managed', ['apt', 'nix', 'docker', 'package', 'image'])
def test_configure_managed_install_refuses_before_lock_or_writes(stack, monkeypatch, managed):
    from hermes_cli import config, image_provenance, update_contract
    t = stack
    if managed == 'package':
        (t.live / 'install-stamp.json').write_text('{"payload": "bundled", "updateMechanism": "app"}')
    elif managed == 'image':
        monkeypatch.setattr(image_provenance, 'read_image_provenance',
                            lambda: SimpleNamespace(valid=True, manager='docker'))
    else:
        (t.live / '.install_method').write_text(managed)
    if managed != 'package':
        assert update_contract.evaluate_update_admission(t.live) is not None
    before_config = config.get_config_path().read_bytes()
    before_refs = git(t.live, 'show-ref')
    directory = t.p.journal_path(t.live).parent
    before_metadata = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    def forbidden(*a):
        pytest.fail('managed configure acquired repository lock')
    original_lock = t.p.repository_lock
    monkeypatch.setattr(t.p, 'repository_lock', forbidden)
    with pytest.raises(t.p.PatchStackError, match='source Git'):
        t.p.configure(t.live, 'lucination', 'upstream/main')
    assert config.get_config_path().read_bytes() == before_config
    assert git(t.live, 'show-ref') == before_refs
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == before_metadata
    # Clearing stale policy is metadata recovery, not code-update admission.
    monkeypatch.setattr(t.p, 'repository_lock', original_lock)
    t.p.clear(t.live)
    assert t.p.read_policy(config.load_config(), t.live) is None
    assert git(t.live, 'show-ref') == before_refs
    assert not t.p.journal_path(t.live).exists()


def test_configuration_persistence_does_not_require_a_clean_worktree(stack):
    t = stack
    t.p.clear(t.live)
    (t.live / 'vault.txt').unlink()
    assert t.p.configure(t.live, 'lucination', 'upstream/main') == t.policy
    assert not t.p.journal_path(t.live).exists()
    assert not (t.live / 'vault.txt').exists()


def test_configuration_is_per_install_and_clear_never_moves_code(stack, tmp_path):
    from hermes_cli.config import load_config
    from hermes_cli.update_channel import channel_record, set_install_channel
    t = stack
    record = channel_record(load_config(), t.live)
    assert record['patch_stack']['remote_url'] == str(t.upstream)
    assert record['patch_stack']['branch'] == 'lucination'
    set_install_channel('main', t.live)
    assert channel_record(load_config(), t.live)['patch_stack'] == record['patch_stack']
    with pytest.raises(ValueError, match='clear'):
        set_install_channel('stable', t.live)
    assert t.p.read_policy(load_config(), tmp_path / 'unrelated') is None
    t.p.clear(t.live)
    assert t.p.read_policy(load_config(), t.live) is None
    assert git(t.live, 'rev-parse', 'HEAD') == t.head
    assert git(t.live, 'branch', '--show-current') == 'lucination'


def test_conflict_keeps_live_state_and_blocks_retry_with_candidate(stack):
    t = stack
    commit(t.writer, 'vault.txt', 'incompatible upstream\n')
    git(t.writer, 'push', '-q', 'origin', 'main')
    before = git(t.live, 'status', '--porcelain')
    with pytest.raises(t.p.PatchStackError) as error:
        t.p.stage(t.live, t.policy)
    assert 'vault.txt' in str(error.value)
    assert 'candidate' in str(error.value)
    assert git(t.live, 'rev-parse', 'HEAD') == t.head
    assert git(t.live, 'rev-parse', t.p.base_ref(t.live)) == t.base
    assert git(t.live, 'status', '--porcelain') == before
    with pytest.raises(t.p.PatchStackError, match='pending'):
        t.p.stage(t.live, t.policy)


@pytest.mark.parametrize('unsafe', ['dirty', 'untracked', 'wrong-branch', 'detached', 'merge',
                                   'missing-base', 'shallow', 'operation', 'remote-change', 'rewind'])
def test_unsafe_observation_refuses_before_staging(stack, unsafe):
    t = stack
    if unsafe == 'dirty':
        (t.live / 'vault.txt').write_text('edit\n', encoding='utf-8')
    elif unsafe == 'untracked':
        (t.live / 'hazard.txt').write_text('local\n', encoding='utf-8')
    elif unsafe == 'wrong-branch':
        git(t.live, 'checkout', '-q', 'main')
    elif unsafe == 'detached':
        git(t.live, 'checkout', '-q', '--detach')
    elif unsafe == 'merge':
        git(t.live, 'checkout', '-qb', 'side', t.base)
        commit(t.live, 'side.txt', 'side\n')
        git(t.live, 'checkout', '-q', 'lucination')
        git(t.live, 'merge', '--no-ff', '-qm', 'merge', 'side')
    elif unsafe == 'missing-base':
        git(t.live, 'update-ref', '-d', t.p.base_ref(t.live))
    elif unsafe == 'shallow':
        (t.live / '.git/shallow').write_text(t.base + '\n', encoding='utf-8')
    elif unsafe == 'operation':
        (t.live / '.git/CHERRY_PICK_HEAD').write_text(t.first, encoding='utf-8')
    elif unsafe == 'remote-change':
        git(t.live, 'remote', 'set-url', 'upstream', str(t.fork))
    elif unsafe == 'rewind':
        git(t.writer, 'checkout', '--orphan', 'rewritten')
        commit(t.writer, 'rewritten.txt', 'rewrite\n')
        git(t.writer, 'push', '-q', '--force', 'origin', 'HEAD:main')
    before = git(t.live, 'rev-parse', 'HEAD')
    with pytest.raises(t.p.PatchStackError):
        t.p.stage(t.live, t.policy)
    assert git(t.live, 'rev-parse', 'HEAD') == before


def test_equivalent_upstream_patch_is_reported_and_zero_stack_stays_configured(stack):
    t = stack
    git(t.writer, 'fetch', '-q', str(t.live), 'lucination')
    git(t.writer, 'cherry-pick', t.first, t.head)
    target = git(t.writer, 'rev-parse', 'HEAD')
    git(t.writer, 'push', '-q', 'origin', 'main')
    candidate = t.p.stage(t.live, t.policy)
    assert set(candidate.dropped) == {t.first, t.head}
    assert candidate.head == target
    t.p.apply(t.live, t.policy, candidate)
    assert git(t.live, 'branch', '--show-current') == 'lucination'


def test_non_equivalent_patch_becoming_empty_stops_recoverably(stack):
    t = stack
    # Combined upstream change includes the patch but has a different patch-id.
    commit(t.writer, 'vault.txt', 'vault patch\n')
    (t.writer / 'extra.txt').write_text('extra\n', encoding='utf-8')
    git(t.writer, 'add', 'extra.txt')
    git(t.writer, 'commit', '--amend', '-qm', 'combined upstream change')
    git(t.writer, 'push', '-q', 'origin', 'main')
    with pytest.raises(t.p.PatchStackError, match='candidate'):
        t.p.stage(t.live, t.policy)
    assert git(t.live, 'rev-parse', 'HEAD') == t.head
