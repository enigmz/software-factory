import json
import subprocess
from pathlib import Path

import pytest

from factory.engine import GateError
from factory.isolation import IsolationConfig, DockerExecutor, copy_source, docker_flags


def test_container_policy_has_no_host_network_or_credentials(tmp_path):
    flags = docker_flags(IsolationConfig(), 'factory-test', tmp_path)
    assert flags[flags.index('--network') + 1] == 'none'
    assert flags[flags.index('--user') + 1] == '10001:10001'
    assert '--read-only' in flags
    assert 'ALL' == flags[flags.index('--cap-drop') + 1]
    assert 'no-new-privileges=true' in flags
    assert '--privileged' not in flags
    assert '--publish' not in flags
    mount = flags[flags.index('--mount') + 1]
    assert mount.endswith('target=/input,readonly')
    assert 'docker.sock' not in ' '.join(flags)
    assert not any('OPENAI' in arg for arg in flags)


def test_snapshot_excludes_git_env_state(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    (source / 'app.py').write_text('value=1')
    (source / '.env').write_text('OPENAI_API_KEY=secret')
    (source / '.ENV.production').write_text('TOKEN=synthetic')
    (source / '.npmrc').write_text('token=synthetic')
    (source / '.git').write_text('gitdir: /controller/repo/.git')
    (source / 'data').mkdir()
    (source / 'data/state.sqlite3').write_text('private')
    (source / 'key.pem').write_text('private key')
    copy_source(source, target)
    assert list(p.name for p in target.iterdir()) == ['app.py']
    assert (target / 'app.py').read_text() == 'value=1'


def test_snapshot_rejects_hardlinks(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    secret = tmp_path / 'secret'
    secret.write_text('synthetic')
    (source / 'linked.txt').hardlink_to(secret)
    with pytest.raises(GateError, match='unlinked'):
        copy_source(source, tmp_path / 'target')


def test_browser_rejects_artifact_traversal(tmp_path):
    from factory.isolated_browser import DockerBrowserSession
    from factory.browser import BrowserConfig
    from factory.preview import PreviewConfig
    session = DockerBrowserSession(tmp_path, 'a' * 40, PreviewConfig(directory='dist'),
        BrowserConfig(base_url='http://127.0.0.1:8765'), IsolationConfig(), tmp_path)
    session.rpc = lambda message: {'files': {'../escape': 'YQ=='}}
    with pytest.raises(GateError, match='artifact name'):
        session._export()
    assert not (tmp_path / 'escape').exists()


def test_snapshot_size_is_bounded(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'big.txt').write_bytes(b'a' * 101)
    with pytest.raises(GateError, match='size'):
        copy_source(source, tmp_path / 'target', max_bytes=100)


def test_missing_docker_never_runs_host_command(tmp_path, monkeypatch):
    seen = []
    def unavailable(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 1, '', 'daemon unavailable')
    monkeypatch.setattr(subprocess, 'run', unavailable)
    runner = DockerExecutor(IsolationConfig(), tmp_path / 'artifacts')
    with pytest.raises(GateError, match='Docker'):
        runner.run(tmp_path, ['python', '-c', 'print(1)'])
    assert all(args[0] == 'docker' for args in seen)


def test_timeout_removes_only_owned_container(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'test.py').write_text('pass')
    seen = []
    def docker(args, **kwargs):
        seen.append(args)
        if args[1:3] == ['version', '--format']:
            return subprocess.CompletedProcess(args, 0, 'linux', '')
        if args[1:3] == ['image', 'inspect']:
            return subprocess.CompletedProcess(args, 0, 'sha256:' + 'a' * 64, '')
        if args[1:3] == ['start', '--attach']:
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', docker)
    runner = DockerExecutor(IsolationConfig(), tmp_path / 'artifacts')
    with pytest.raises(GateError, match='timeout'):
        runner.run(source, ['python', 'test.py'])
    created = next(args for args in seen if args[1] == 'create')
    name = created[created.index('--name') + 1]
    assert ['docker', 'rm', '--force', name] in seen
    assert name.startswith('factory-')


def test_worker_defaults_to_isolation_and_local_is_explicit():
    from factory.worker import Config
    config = Config(repo='repo', models={'default': 'test'}, checks={})
    assert config.isolation.mode == 'docker'
