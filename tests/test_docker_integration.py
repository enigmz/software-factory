"""Opt-in tests against the real Docker Linux engine and prebuilt worker image."""
import os
import subprocess

import pytest

from factory.engine import GateError
from factory.isolation import DockerExecutor, IsolationConfig

pytestmark = pytest.mark.skipif(os.environ.get('FACTORY_TEST_DOCKER') != '1',
                              reason='Set FACTORY_TEST_DOCKER=1 with the worker image built')


def test_real_container_denies_secrets_network_and_host_writes(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / '.env').write_text('SECRET=synthetic-secret')
    (source / 'app.txt').write_text('original')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-controller-secret')
    script = '''import os, socket
from pathlib import Path
assert os.getuid() == 10001
assert 'OPENAI_API_KEY' not in os.environ
assert not Path('/input/.env').exists()
assert not Path('/var/run/docker.sock').exists()
for path in ['/input/app.txt', '/opt/runner.py', '/etc/host-write-test']:
    try:
        Path(path).write_text('unauthorized')
    except OSError:
        pass
    else:
        raise AssertionError(path)
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('network allowed')
Path('/workspace/app.txt').write_text('ephemeral')
print('isolation-ok')
'''
    runner = DockerExecutor(IsolationConfig(), tmp_path / 'artifacts')
    result = runner.run(source, ['python', '-c', script])
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'isolation-ok' in result.stdout
    assert (source / 'app.txt').read_text() == 'original'


def test_real_timeout_removes_container(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    runner = DockerExecutor(IsolationConfig(timeout_seconds=5), tmp_path / 'artifacts')
    names = []
    original = runner.remove
    def remove(name):
        names.append(name)
        original(name)
    runner.remove = remove
    with pytest.raises(GateError, match='timeout'):
        runner.run(source, ['python', '-c', 'import time; time.sleep(60)'])
    assert len(names) == 1
    assert runner.docker(['inspect', names[0]], check=False).returncode != 0


def test_real_isolated_browser(tmp_path):
    from factory.browser import BrowserConfig
    from factory.preview import PreviewConfig
    from factory.isolated_browser import DockerBrowserSession
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.email', 'fixture@example.test')
    git('config', 'user.name', 'Fixture')
    (repo / 'dist').mkdir()
    (repo / 'dist/index.html').write_text('<!doctype html><title>Fixture</title><h1>Isolated preview</h1>')
    git('add', '.')
    git('commit', '-m', 'fixture')
    commit = git('rev-parse', 'HEAD')
    with DockerBrowserSession(repo, commit, PreviewConfig(directory='dist', startup_timeout=30),
            BrowserConfig(base_url='http://127.0.0.1:8765'), IsolationConfig(),
            tmp_path / 'artifacts') as session:
        session.call('screenshot', {})
        session.call('viewport', {'device': 'mobile'})
        session.call('screenshot', {})
        assert session.complete()
    assert (session.directory / 'manifest.json').is_file()
    assert (session.directory / 'trace.zip').is_file()
    assert len(list(session.directory.glob('*.png'))) == 2
