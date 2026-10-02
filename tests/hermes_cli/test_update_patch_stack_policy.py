"""Patch-stack policy boundaries."""
import importlib
import pytest


def test_explicit_policy_preserves_literal_identity():
    p = importlib.import_module('hermes_cli.update_patch_stack')
    policy = p.Policy.parse('lucination', 'upstream/main', '/local/upstream.git')
    assert policy.branch == 'lucination'
    assert policy.base_remote == 'upstream'
    assert policy.base_branch == 'main'
    assert policy.remote_url == '/local/upstream.git'
    for branch, base in [(' lucination', 'upstream/main'), ('-x', 'upstream/main'),
                         ('x', 'upstream/main~1'), ('x', 'upstream'), ('x', 'upstream/dev')]:
        with pytest.raises(ValueError):
            p.Policy.parse(branch, base, '/local/upstream.git')


@pytest.mark.parametrize('config', [{'update': 'bad'}, {'update': {'installs': []}}])
def test_read_policy_refuses_unreadable_install_scope(tmp_path, config):
    from hermes_cli.update_patch_stack import read_policy, PatchStackError
    with pytest.raises(PatchStackError):
        read_policy(config, tmp_path)


@pytest.mark.parametrize('data', [None, {}, {'branch': 'x', 'base_remote': 'upstream',
    'base_branch': 'main', 'remote_url': '/remote', 'anchor_sha': ''},
    {'branch': 'x/.hidden', 'base_remote': 'upstream', 'base_branch': 'main',
     'remote_url': '/remote', 'anchor_sha': 'a' * 40}])
def test_read_policy_never_silently_disables_malformed_policy(tmp_path, data):
    from hermes_cli.update_channel import install_id
    from hermes_cli.update_patch_stack import read_policy, PatchStackError
    config = {'update': {'installs': {install_id(tmp_path): {'patch_stack': data}}}}
    with pytest.raises(PatchStackError):
        read_policy(config, tmp_path)
