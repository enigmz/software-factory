import json
import subprocess
import sys

import httpx
import pytest

from factory.engine import GateError
from factory.preview import ManagedPreview, PreviewConfig


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'dist').mkdir()
    (repo / 'brain').mkdir()
    (repo / 'brain/architecture.md').write_text('Static HTML preview. Preserve contrast.')
    page = repo / 'dist/index.html'
    page.write_text('<h1>First revision</h1>')
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'first')
    first = git('rev-parse', 'HEAD')
    page.write_text('<h1>Second revision</h1>')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'second')
    return repo, first, git('rev-parse', 'HEAD')


def test_managed_preview_binds_exact_commit_and_closes(repository, tmp_path):
    repo, first, second = repository
    with httpx.Client(trust_env=False, timeout=2) as client:
        with ManagedPreview(repo, first, PreviewConfig(directory='dist'), tmp_path / 'artifacts') as preview:
            url = preview.url
            assert client.get(url + '/.factory/revision').json() == {'commit': first}
            assert 'First revision' in client.get(url).text
        with pytest.raises(httpx.TransportError):
            client.get(url)
        assert not preview.thread.is_alive()
        manifest = json.loads(preview.manifest.read_text())
        assert manifest['state'] == 'stopped'
        assert manifest['commit'] == first
        with ManagedPreview(repo, second, PreviewConfig(directory='dist'), tmp_path / 'artifacts') as next_preview:
            assert client.get(next_preview.url + '/.factory/revision').json() == {'commit': second}
            assert 'Second revision' in client.get(next_preview.url).text


def test_exception_closes_owned_preview_without_stopping_another(repository, tmp_path):
    repo, first, second = repository
    with ManagedPreview(repo, first, PreviewConfig(directory='dist'), tmp_path) as existing:
        with pytest.raises(RuntimeError, match='agent failed'):
            with ManagedPreview(repo, second, PreviewConfig(directory='dist'), tmp_path) as failing:
                assert existing.url != failing.url
                raise RuntimeError('agent failed')
        assert not failing.thread.is_alive()
        with httpx.Client(trust_env=False) as client:
            assert client.get(existing.url).status_code == 200
        assert json.loads(failing.manifest.read_text())['outcome'] == 'error'


def test_wrong_sha_and_missing_directory_do_not_start_server(repository, tmp_path):
    repo, _, _ = repository
    with pytest.raises(GateError):
        with ManagedPreview(repo, 'a' * 40, PreviewConfig(directory='dist'), tmp_path):
            pytest.fail('Unknown commit must not become ready')


def test_readiness_failure_releases_socket(repository, tmp_path, monkeypatch):
    repo, first, _ = repository
    preview = ManagedPreview(repo, first, PreviewConfig(directory='dist'), tmp_path)
    def wrong_revision():
        raise GateError('Preview revision mismatch')
    monkeypatch.setattr(preview, '_ready', wrong_revision)
    with pytest.raises(GateError, match='revision'):
        with preview:
            pytest.fail('Mismatch must not become ready')
    assert not preview.thread.is_alive()
    with httpx.Client(trust_env=False, timeout=1) as client:
        with pytest.raises(httpx.TransportError):
            client.get(preview.url)


def test_readiness_timeout_closes_server(repository, tmp_path, monkeypatch):
    repo, first, _ = repository
    preview = ManagedPreview(repo, first, PreviewConfig(directory='dist', startup_timeout=0.1), tmp_path)
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError('fixture unavailable')
    monkeypatch.setattr(httpx.Client, 'get', unavailable)
    with pytest.raises(GateError, match='timed out'):
        with preview:
            pytest.fail('Unavailable preview must not become ready')
    assert not preview.thread.is_alive()


@pytest.mark.browser
def test_worker_starts_preview_for_visual_checks_and_closes_it(repository, tmp_path):
    from factory.browser import BrowserConfig
    from factory.contracts import Build, Review, Spec
    from factory.worker import Config, Worker
    repo, first, second = repository
    calls = []
    class Agent:
        def __init__(self, model): pass
        def run(self, context, contract, tools):
            calls.append(context['role'])
            if context['role'] == 'ui':
                tools.call('screenshot', {})
                tools.call('viewport', {'device': 'mobile'})
                tools.call('screenshot', {})
            return Review(role=context['role'], commit=context['commit'], verdict='PASS',
                          summary='Test fixture observations', evidence=['fixture'])
    config = Config(repo=str(repo), database=str(tmp_path / 'state.db'),
        worktrees=str(tmp_path / 'trees'), artifacts=str(tmp_path / 'artifacts'),
        models={'default': 'test-double'}, isolation={'mode': 'local'}, checks={key: [sys.executable, '-c', 'print("fixture check")']
            for key in ('tests', 'lint', 'acceptance')},
        browser=BrowserConfig(base_url='http://127.0.0.1:1'), preview=PreviewConfig(directory='dist'))
    worker = Worker(config, agent_factory=Agent)
    task = worker.factory.submit('prompt', 'managed-test', str(repo), 'Change landing')
    task_id = task['id']
    worker.factory.set_spec(task_id, Spec(objective='Change landing', acceptance=['Second revision'],
        exclusions=[], rollback='Revert', brain_revision=first, tasks=[{'id': 'color',
            'description': 'Update landing', 'acceptance': ['Second revision'], 'validation': 'visual'}]))
    tree = worker.workspace.prepare(task_id, first)
    worker.workspace.git('merge', '--ff-only', second, cwd=tree['path'])
    worker.factory.set_build(task_id, Build(commit=second, base_commit=first, brain_revision=first,
        changed_files=['dist/index.html'], summary='Second version'))
    journal = tmp_path / 'artifacts' / task_id / '0' / 'checks.json'
    journal.parent.mkdir(parents=True)
    journal.write_text('[]')
    assert worker.step(task_id)['state'] == 'REVIEW_PENDING'
    assert worker.step(task_id)['state'] == 'RISK_PENDING'
    assert calls == ['ui', 'security', 'ux', 'ui', 'consistency']
    manifests = list((tmp_path / 'artifacts/previews').glob('*/preview.json'))
    assert len(manifests) == 2
    with httpx.Client(trust_env=False, timeout=1) as client:
        for manifest in manifests:
            record = json.loads(manifest.read_text())
            assert record['commit'] == second
            assert record['state'] == 'stopped'
            with pytest.raises(httpx.TransportError):
                client.get(record['url'])


@pytest.mark.browser
def test_agent_error_does_not_leave_managed_browser_server(repository, tmp_path):
    from factory.browser import BrowserConfig
    from factory.managed_browser import ManagedBrowserHarness
    repo, first, _ = repository
    class Agent:
        def run(self, *args):
            raise GateError('Model unavailable')
    harness = ManagedBrowserHarness(repo, PreviewConfig(directory='dist'),
        BrowserConfig(base_url='http://127.0.0.1:1'), tmp_path, lambda: Agent())
    with pytest.raises(GateError, match='Model unavailable'):
        harness.verify_task(first, {'id': 'test', 'acceptance': ['Visible heading']})
    record = json.loads(next((tmp_path / 'previews').glob('*/preview.json')).read_text())
    assert record['state'] == 'stopped'
    assert record['outcome'] == 'error'
    with httpx.Client(trust_env=False, timeout=1) as client:
        with pytest.raises(httpx.TransportError):
            client.get(record['url'])
