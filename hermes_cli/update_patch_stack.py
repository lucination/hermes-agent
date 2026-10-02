"""Opt-in source patch stacks: immutable policy and guarded Git effects.

Recovery is deliberately manual. Never delete a pending journal to retry, run
reset --hard, clean -fdx, or automatically overwrite a partial checkout. Stop
all Hermes/updater processes, orphaned completion children, PM workers, build
subprocesses, service-manager restart jobs and manual Git writers first. Owner
death or a released OS lock does not establish quiescence: completion reservations
never expire. Copy the entire checkout AND the absolute git-common-dir (including candidate, journal and
recovery refs) to separate backup directories before changing anything.

Safe inspection (substitute the journal's literal values, not branch guesses):
    git rev-parse --path-format=absolute --git-common-dir
    git status --porcelain=v1 --untracked-files=all --ignored
    git show <recovery_head>
    git show <recovery_base>
The journal's original/base describe the old state; head/target describe the
candidate. Record HEAD, the configured branch ref and base ref separately.
If immediate access to old code is needed, create a NEW, nonexistent directory:
    git clone --no-local --no-checkout <checkout> <new-recovery-checkout>
    git -C <new-recovery-checkout> -c core.hooksPath=/dev/null checkout --detach <original>
This leaves all original files (including ignored files), refs and the pending
barrier intact. Do not point services at that copy without separately checking
its dependencies/configuration. Reconcile the original checkout and both refs
under exclusive operator control, preserving any concurrent edits, before an
operator archives/removes the journal; there is no automated recovery API.

Locking requires POSIX flock and directory fsync. Windows is explicitly
unsupported/fail-closed; it must not silently fall back to a process-local lock.
OS locks do not exclude editors/manual Git or make filesystem writes atomic.
Git apply --index rejects collisions at its publication checks, but concurrent
writers inside Git's check/write syscall window are not a supported guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import os
from pathlib import Path
import subprocess
from uuid import uuid4
import json
from contextlib import contextmanager
from functools import wraps
import threading

_locks = threading.local()


def common_directory(root: Path) -> Path:
    return Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()).resolve()


@contextmanager
def repository_lock(root: Path):
    """Nonblocking OS lock shared by every worktree/home; reentrant per thread.

    Orchestrators must hold this around the complete stage/apply sequence.
    Individual APIs also acquire it. This does not lock out manual Git/editors.
    """
    directory = common_directory(root) / 'hermes-patch-stacks'
    directory.mkdir(exist_ok=True)
    key = (os.getpid(), str(directory))
    held = getattr(_locks, 'held', set())
    if key in held:
        yield
        return
    if os.name != 'posix':
        raise PatchStackError('Repository locking unsupported; fail closed')
    try:
        # Persist the journal directory's entry before any transaction writes.
        # Sync even an existing entry: a prior failed attempt may have created it.
        sync_directory(directory.parent)
    except OSError as exc:
        raise PatchStackError(f'Journal directory persistence failed: {exc}') from exc
    with (directory / 'repository.lock').open('a+b') as stream:
        try:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            raise PatchStackError('Patch-stack repository locked or locking unavailable') from exc
        _locks.held = held | {key}
        try:
            yield
        finally:
            _locks.held = held
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def locked(function):
    @wraps(function)
    def run(root, *args, **kwargs):
        with repository_lock(root):
            return function(root, *args, **kwargs)
    return run


class PatchStackError(ValueError):
    """Refusal with retained evidence; never a reason for ZIP fallback."""


def journal_path(root: Path) -> Path:
    from hermes_cli.update_channel import install_id
    return common_directory(root) / 'hermes-patch-stacks' / (install_id(root) + '.json')


def write_journal(root: Path, data: dict) -> None:
    path = journal_path(root)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_publication(root: Path, *, worktree: bool = True) -> None:
    """Persist publication before releasing its recovery barrier.

    Requires a local POSIX filesystem honoring file/directory fsync. Network
    filesystems, lying storage caches, external object alternates and concurrent
    manual writers are not covered. Git's own fsync is defense in depth; this
    explicit pass also catches OS persistence errors rather than hiding them in
    Git warnings. Do not traverse candidate checkouts or ignored file contents.
    """
    import stat

    def sync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PatchStackError(f'Cannot persist nonregular publication file: {path}')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def walk_error(error):
        raise error

    try:
        common = common_directory(root)
        if (common / 'objects/info/alternates').exists():
            raise PatchStackError('External object alternates unsupported for durable publication')
        gitdir = Path(git(root, 'rev-parse', '--absolute-git-dir').stdout.strip())
        directories = set()
        for metadata in dict.fromkeys((common, gitdir)):
            for directory, children, files in os.walk(metadata, onerror=walk_error):
                path = Path(directory)
                if path == common:
                    children[:] = [name for name in children if name != 'hermes-patch-stacks']
                directories.add(path)
                for name in files:
                    sync_file(path / name)
        if worktree:
            for name in git(root, 'ls-files', '-z').stdout.split('\0'):
                if not name:
                    continue
                path = root / name
                if not path.is_symlink():
                    sync_file(path)
            # Include surviving parents of deleted tracked files, even when
            # their remaining contents are entirely ignored/untracked.
            for directory, children, _ in os.walk(root, onerror=walk_error):
                path = Path(directory)
                if path == root:
                    children[:] = [name for name in children if name != '.git']
                directories.add(path)
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            sync_directory(directory)
    except OSError as exc:
        raise PatchStackError(f'Publication persistence failed; pending journal retained: {exc}') from exc


def finish_journal(root: Path) -> None:
    path = journal_path(root)
    data = json.loads(path.read_text(encoding='utf-8'))
    try:
        # Detect unavailable directory persistence before dropping the barrier.
        sync_directory(path.parent)
        path.unlink()
        sync_directory(path.parent)
    except OSError as exc:
        if not path.exists():
            try:
                write_journal(root, data)
            except OSError as restore_error:
                raise PatchStackError(f'Journal removal persistence failed; barrier restoration failed: {restore_error}') from exc
        raise PatchStackError(f'Journal removal persistence failed; pending journal retained: {exc}') from exc


def require_no_pending(root: Path) -> None:
    pending = sorted((common_directory(root) / 'hermes-patch-stacks').glob('*.json'))
    if pending:
        raise PatchStackError(f'Patch-stack pending transaction: {pending[0]}. Inspect journal and candidate; explicit recovery required.')


def git(root: Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_EDITOR='false', GIT_SEQUENCE_EDITOR='false',
               GIT_NO_REPLACE_OBJECTS='1', GIT_GRAFT_FILE=os.devnull, GIT_OPTIONAL_LOCKS='0',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ATTR_NOSYSTEM='1', GCM_INTERACTIVE='Never', SSH_ASKPASS='false',
               SSH_ASKPASS_REQUIRE='never')
    settings = ('core.sshCommand=ssh -oBatchMode=yes', 'protocol.ext.allow=never',
                'credential.interactive=false', 'core.hooksPath=' + os.devnull, 'commit.gpgsign=false',
                'tag.gpgsign=false', 'rebase.autoStash=false', 'rebase.updateRefs=false',
                'rebase.autosquash=false', 'rebase.rebaseMerges=false', 'rebase.forkPoint=false',
                'rerere.enabled=false', 'rerere.autoupdate=false', 'core.fsmonitor=false',
                'core.editor=false', 'sequence.editor=false', 'core.attributesFile=' + os.devnull,
                'fetch.recurseSubmodules=false', 'submodule.recurse=false',
                'maintenance.auto=false', 'gc.auto=0', 'core.fsync=all', 'core.fsyncMethod=fsync')
    try:
        return subprocess.run(['git', *[part for setting in settings for part in ('-c', setting)], *args], cwd=root, env=env,
                              stdin=subprocess.DEVNULL if input is None else None,
                              input=input, text=True, capture_output=True, check=check, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        detail = getattr(exc, 'stderr', '') or str(exc)
        raise PatchStackError(f'Git failed closed: {detail}') from exc


def sha(root: Path, ref: str) -> str:
    return git(root, 'rev-parse', '--verify', ref + '^{commit}').stdout.strip()


def base_ref(root: Path) -> str:
    from hermes_cli.update_channel import install_id
    return f'refs/hermes-patch-stacks/{install_id(root)}/base'


def read_policy(config: dict, root: Path) -> Policy | None:
    from hermes_cli.update_channel import channel_record, install_id
    if not isinstance(config, dict):
        raise PatchStackError('Configuration must be a mapping')
    update = config.get('update', {})
    if not isinstance(update, dict) or not isinstance(update.get('installs', {}), dict):
        raise PatchStackError('update / update.installs must be mappings')
    records = update.get('installs', {})
    if install_id(root) in records and not isinstance(records[install_id(root)], dict):
        raise PatchStackError('Install record must be a mapping')
    record = channel_record(config, root)
    data = record.get('patch_stack')
    if 'patch_stack' not in record:
        return None
    if not isinstance(data, dict) or record.get('channel', 'main') != 'main':
        raise PatchStackError('Malformed/incompatible patch-stack policy; clear explicitly')
    try:
        if not data.get('anchor_sha'):
            raise ValueError('Missing anchor')
        return Policy.parse(data['branch'], data['base_remote'] + '/' + data['base_branch'], data['remote_url'], data['anchor_sha'])
    except (KeyError, TypeError, ValueError) as exc:
        raise PatchStackError('Malformed patch-stack policy; clear explicitly') from exc


def _write_policy(root: Path, policy: Policy | None) -> None:
    from dataclasses import asdict
    from hermes_cli.config import get_config_path, require_readable_config_before_write
    from hermes_cli.update_channel import channel_record, install_id, _channel_write_lock
    from utils import atomic_roundtrip_yaml_update
    path = get_config_path()
    with _channel_write_lock(path):
        config = require_readable_config_before_write(path)
        for mapping in (config.get('update', {}), config.get('update', {}).get('installs', {})
                        if isinstance(config.get('update', {}), dict) else None):
            if not isinstance(mapping, dict):
                raise PatchStackError('update / update.installs must be mappings')
        record = dict(channel_record(config, root))
        record['path'] = str(root.resolve())
        if policy is None:
            record.pop('patch_stack', None)
        else:
            record['patch_stack'] = asdict(policy)
            record.setdefault('channel', 'main')
        atomic_roundtrip_yaml_update(path, f'update.installs.{install_id(root)}', record)


@locked
def clear(root: Path) -> None:
    require_no_pending(root)
    _write_policy(root, None)


def configure(root: Path, branch: str, base: str) -> Policy:
    # Managed admission precedes even repository lock metadata creation. A Git
    # directory is not permission to rewrite a package/image-owned install.
    from hermes_cli.update_contract import evaluate_update_admission
    from hermes_cli.update_channel import _read_stamp, _package_channel
    stamp = _read_stamp(root)
    if (evaluate_update_admission(root) is not None or _package_channel(stamp)
            or stamp.get('updateMechanism') not in (None, 'self')
            or not (root / '.git').exists()):
        raise PatchStackError('Patch stacks support source Git installs only')
    return _configure(root, branch, base)


@locked
def _configure(root: Path, branch: str, base: str) -> Policy:
    require_no_pending(root)
    from hermes_cli.config import load_config
    from hermes_cli.update_channel import resolve_update_channel
    if resolve_update_channel(load_config(), root) != 'main':
        raise PatchStackError('Patch stacks support main channel only')
    # Validate tokens before giving any user-supplied name to Git.
    Policy.parse(branch, base, 'validate')
    remote = base.partition('/')[0]
    policy = Policy.parse(branch, base, git(root, 'remote', 'get-url', remote).stdout.strip())
    git(root, 'check-ref-format', 'refs/heads/' + branch)
    if git(root, 'rev-parse', '--is-shallow-repository').stdout.strip() != 'false':
        raise PatchStackError('shallow ancestry: unshallow manually before setup')
    anchor = git(root, 'merge-base', '--all', f'refs/heads/{branch}', f'refs/remotes/{base}').stdout.splitlines()
    if len(anchor) != 1:
        raise PatchStackError('Setup requires a unique verifiable merge base')
    if git(root, 'rev-list', '--merges', f'{anchor[0]}..refs/heads/{branch}').stdout.strip():
        raise PatchStackError('merge-containing stack')
    from dataclasses import replace
    policy = replace(policy, anchor_sha=anchor[0])
    previous = git(root, 'rev-parse', '--verify', base_ref(root), check=False)
    expected = previous.stdout.strip() if previous.returncode == 0 else '0' * len(anchor[0])
    write_journal(root, {'phase': 'configuring', 'branch': branch,
                         'base': expected, 'target': anchor[0], 'remote_url': policy.remote_url})
    git(root, 'update-ref', base_ref(root), anchor[0], expected)
    _write_policy(root, policy)
    from hermes_cli.config import load_config
    if read_policy(load_config(), root) != policy:
        raise PatchStackError('Configuration verification failed; explicit recovery required')
    sync_publication(root, worktree=False)
    finish_journal(root)
    return policy


@dataclass(frozen=True)
class Staged:
    path: Path
    original: str
    base: str
    target: str
    head: str
    dropped: tuple[str, ...] = ()


@dataclass(frozen=True)
class Applied:
    head: str
    base: str


@dataclass(frozen=True)
class Observation:
    branch: str
    head: str
    base: str
    remote_url: str
    hazards: tuple[str, ...]


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class NoOp:
    head: str


@dataclass(frozen=True)
class Rebase:
    original: str
    base: str
    target: str


def plan(policy: Policy, observation: Observation, target: str, *, forward: bool) -> Refused | NoOp | Rebase:
    if observation.hazards:
        return Refused(', '.join(observation.hazards))
    if observation.branch != policy.branch:
        return Refused(f'Check out {policy.branch}; detached or other branch is not accepted')
    if observation.remote_url != policy.remote_url:
        return Refused('Configured remote identity changed; explicit reconfiguration required')
    if not forward:
        return Refused('Upstream rewind/divergence; explicit reconfiguration required')
    if observation.base == target:
        return NoOp(observation.head)
    return Rebase(observation.head, observation.base, target)


def observe(root: Path, policy: Policy) -> Observation:
    head = sha(root, 'HEAD')
    base = sha(root, base_ref(root))
    hazards = []
    filters = git(root, 'config', '--get-regexp', r'^filter\..*\.(clean|smudge|process)$', check=False)
    if filters.returncode not in (0, 1):
        raise PatchStackError('Cannot inspect external filter configuration')
    if filters.stdout.strip():
        hazards.append('external checkout filter configured')
    drivers = git(root, 'config', '--get-regexp', r'^merge\..*\.driver$', check=False)
    if drivers.returncode not in (0, 1):
        raise PatchStackError('Cannot inspect custom merge driver configuration')
    if drivers.stdout.strip():
        hazards.append('custom merge driver configured')
    entries = git(root, 'ls-files', '-v', '-z').stdout.split('\0')
    if any(entry and (entry[0].islower() or entry[0] == 'S') for entry in entries):
        hazards.append('hidden index flags (assume-unchanged / skip-worktree)')
    if git(root, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
        hazards.append('dirty or untracked checkout')
    if git(root, 'rev-parse', '--is-shallow-repository').stdout.strip() != 'false':
        hazards.append('shallow ancestry')
    directory = Path(git(root, 'rev-parse', '--absolute-git-dir').stdout.strip())
    if any((directory / marker).exists() for marker in (
            'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer')):
        hazards.append('Git operation in progress')
    if git(root, 'merge-base', '--is-ancestor', base, head, check=False).returncode:
        hazards.append('recorded base is not an ancestor of patch head')
    if git(root, 'rev-list', '--merges', f'{base}..{head}').stdout.strip():
        hazards.append('merge-containing stack')
    return Observation(git(root, 'branch', '--show-current').stdout.strip(), head, base,
                       git(root, 'remote', 'get-url', policy.base_remote).stdout.strip(), tuple(hazards))


def require_current_policy(root: Path, policy: Policy) -> None:
    from hermes_cli.config import load_config
    if read_policy(load_config(), root) != policy:
        raise PatchStackError('Explicit install policy missing or changed; reconfigure explicitly')


STARTUP_FILES = ('hermes_cli/main.py', 'hermes_cli/config.py', 'hermes_cli/__init__.py',
                 'hermes_cli/web_server.py', 'cli.py', 'run_agent.py', 'model_tools.py',
                 'toolsets.py', 'hermes_constants.py', 'hermes_bootstrap.py',
                 'hermes_cli/update_patch_stack.py', 'hermes_cli/update_channel.py',
                 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_check.py',
                 'hermes_cli/update_completion.py', 'hermes_cli/source_completion.py',
                 'hermes_cli/source_check.py', 'hermes_cli/venv_sync.py',
                 'hermes_cli/update_receipt.py')


def validate_candidate(path: Path) -> None:
    """Require tracked regular startup modules; parse without executing candidate code."""
    for name in STARTUP_FILES:
        source = path / name
        try:
            if source.is_symlink() or not source.is_file():
                raise ValueError('missing or nonregular module')
            git(path, 'ls-files', '--error-unmatch', '--', name)
            compile(source.read_bytes(), str(source), 'exec')
        except (OSError, SyntaxError, ValueError) as exc:
            raise PatchStackError(f'Invalid candidate startup module {name}: {exc}') from exc


@locked
def stage(root: Path, policy: Policy) -> Staged:
    require_no_pending(root)
    require_current_policy(root, policy)
    observation = observe(root, policy)
    preliminary = plan(policy, observation, observation.base, forward=True)
    if isinstance(preliminary, Refused):
        raise PatchStackError(preliminary.reason)
    old, head = observation.base, observation.head
    ref = base_ref(root).removesuffix('/base') + '/target'
    git(root, 'fetch', '--no-tags', policy.base_remote,
        f'+refs/heads/{policy.base_branch}:{ref}')
    target = sha(root, ref)
    decision = plan(policy, observation, target,
                    forward=git(root, 'merge-base', '--is-ancestor', old, target, check=False).returncode == 0)
    if isinstance(decision, Refused):
        raise PatchStackError(decision.reason)
    if isinstance(decision, NoOp):
        return Staged(root, head, old, target, head)
    directory = common_directory(root) / 'hermes-patch-stacks' / uuid4().hex
    directory.mkdir(parents=True)
    path = directory / 'candidate'
    data = {'phase': 'staging', 'candidate': str(path), 'original': head, 'base': old,
            'target': target, 'branch': policy.branch}
    recovery = base_ref(root).removesuffix('/base') + '/recovery/' + directory.name
    data.update(recovery_head=recovery + '/head', recovery_base=recovery + '/base')
    write_journal(root, data)
    git(root, 'update-ref', '--stdin', input=f"start\ncreate {recovery}/head {head}\ncreate {recovery}/base {old}\nprepare\ncommit\n")
    git(root, 'worktree', 'add', '--detach', str(path), head)
    comparisons = git(root, 'cherry', target, head, old).stdout.splitlines()
    originals = git(root, 'rev-list', '--reverse', f'{old}..{head}').stdout.splitlines()
    equivalents = {line.split()[1] for line in comparisons if line.startswith('- ')}
    equivalents.update(commit for commit in originals
                       if git(root, 'merge-base', '--is-ancestor', commit, target, check=False).returncode == 0)
    dropped = tuple(commit for commit in originals if commit in equivalents)
    result = git(path, 'rebase', '--no-reapply-cherry-picks', '--empty=stop', '--onto', target, old, check=False)
    while result.returncode:
        stopped = git(path, 'rev-parse', '--verify', 'REBASE_HEAD', check=False).stdout.strip()
        conflicts = git(path, 'diff', '--name-only', '--diff-filter=U').stdout.strip()
        if stopped not in equivalents or conflicts:
            break
        result = git(path, 'rebase', '--skip', check=False)
    if result.returncode:
        paths = git(path, 'diff', '--name-only', '--diff-filter=U').stdout.strip()
        write_journal(root, {**data, 'phase': 'conflict', 'paths': paths})
        raise PatchStackError(f'Rebase stopped in candidate {path}: {paths}\n{result.stderr}')
    try:
        validate_candidate(path)
    except PatchStackError as exc:
        write_journal(root, {**data, 'phase': 'invalid-candidate', 'error': str(exc)})
        raise
    candidate = Staged(path, head, old, target, sha(path, 'HEAD'), dropped)
    write_journal(root, {**data, 'phase': 'staged', 'head': candidate.head})
    return candidate


@locked
def apply(root: Path, policy: Policy, candidate: Staged) -> Applied:
    require_current_policy(root, policy)
    observation = observe(root, policy)
    decision = plan(policy, observation, candidate.target, forward=True)
    if (isinstance(decision, Refused) or observation.head != candidate.original
            or observation.base != candidate.base):
        raise PatchStackError('Publication refused: checkout or base changed')
    if candidate.path == root:
        require_no_pending(root)
        if candidate.head != candidate.original or candidate.target != candidate.base:
            raise PatchStackError('Invalid no-op candidate')
        write_journal(root, {'phase': 'applied', 'branch': policy.branch,
                             'original': candidate.original, 'base': candidate.base,
                             'target': candidate.target, 'head': candidate.head})
        return Applied(candidate.head, candidate.target)
    try:
        data = json.loads(journal_path(root).read_text())
    except (OSError, ValueError) as exc:
        raise PatchStackError('Missing or invalid staged journal') from exc
    expected = dict(candidate=str(candidate.path), original=candidate.original,
                    base=candidate.base, target=candidate.target, head=candidate.head,
                    branch=policy.branch, phase='staged')
    if any(data.get(key) != value for key, value in expected.items()):
        raise PatchStackError('Candidate does not match pending staged journal')
    if (sha(candidate.path, 'HEAD') != candidate.head
            or git(candidate.path, 'status', '--porcelain', '--untracked-files=all').stdout.strip()):
        raise PatchStackError('Candidate changed since staging')
    validate_candidate(candidate.path)
    # Unlike read-tree -u, apply refuses existing untracked paths even when
    # ignored. Check before moving refs, then repeat Git's checks at publication
    # so an ignored collision introduced after this preflight is not discarded.
    patch = git(root, 'diff', '--binary', '--full-index', '--no-renames',
                '--no-ext-diff', '--no-textconv', candidate.original, candidate.head).stdout
    git(root, 'apply', '--check', '--index', '--allow-empty', input=patch)
    # Both refs commit atomically, before touching checkout files. An interrupted
    # filesystem update leaves a durable barrier, never an automatic reset.
    write_journal(root, {**data, 'phase': 'publishing-refs'})
    git(root, 'update-ref', '--stdin', input=(
        f'start\nupdate refs/heads/{policy.branch} {candidate.head} {candidate.original}\n'
        f'update {base_ref(root)} {candidate.target} {candidate.base}\nprepare\ncommit\n'))
    write_journal(root, {**data, 'phase': 'publishing-files'})
    if (git(root, 'branch', '--show-current').stdout.strip() != policy.branch
            or sha(root, 'HEAD') != candidate.head or sha(root, base_ref(root)) != candidate.target):
        raise PatchStackError('Refs changed during publication; explicit recovery required')
    git(root, 'apply', '--index', '--allow-empty', input=patch)
    if (git(root, 'branch', '--show-current').stdout.strip() != policy.branch
            or sha(root, 'HEAD') != candidate.head or sha(root, base_ref(root)) != candidate.target
            or git(root, 'status', '--porcelain', '--untracked-files=all').stdout.strip()):
        raise PatchStackError('Partial publication; explicit recovery required')
    write_journal(root, {**data, 'phase': 'applied'})
    sync_publication(root)
    # The orchestrator owns completion. Retain this barrier across receipt and
    # request preparation; it is atomically replaced by the completion phase.
    return Applied(candidate.head, candidate.target)


@dataclass(frozen=True)
class Policy:
    branch: str
    base_remote: str
    base_branch: str
    remote_url: str
    anchor_sha: str = ''

    @classmethod
    def parse(cls, branch: str, base: str, remote_url: str, anchor_sha: str = '') -> Policy:
        def valid(value: str) -> bool:
            return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', value)) and not any(
                part in value for part in ('..', '//', '@{')) and not value.endswith(('/', '.')) and all(
                    not part.startswith('.') and not part.endswith('.lock') for part in value.split('/'))
        if not isinstance(base, str) or not isinstance(remote_url, str) or not isinstance(anchor_sha, str):
            raise ValueError('Policy values must be literal strings')
        remote, sep, target = base.partition('/')
        if not valid(branch) or not sep or not valid(remote) or '/' in remote or target != 'main' or not remote_url:
            raise ValueError('Patch stack requires a literal branch and REMOTE/main base')
        if anchor_sha and not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', anchor_sha):
            raise ValueError('Anchor must be a full commit SHA')
        return cls(branch, remote, target, remote_url, anchor_sha)
