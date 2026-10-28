import json
import subprocess
import sys

import httpx
import pytest

from factory.contracts import Risk
from factory.engine import GateError
from factory.openai_agents import OpenAIAgent
from factory.runtime import RepositoryTools


def test_openai_has_fresh_context_and_validates_output():
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'commit': 'a' * 40, 'level': 'low', 'reasons': ['Cosmetic'],
                'reversible': True, 'uncertain': False})}]}]})
    agent = OpenAIAgent('test-model', api_key='test', transport=httpx.MockTransport(respond))
    for role in ('risk', 'security'):
        agent.run({'role': role}, Risk)
    assert len(requests) == 2
    assert 'previous_response_id' not in requests[1]
    assert requests[1]['store'] is False
    assert requests[1]['text']['format']['strict'] is True


def test_refusal_and_incomplete_are_not_success():
    for output in ({'status': 'incomplete', 'output': []},
                   {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No'}]}]}):
        agent = OpenAIAgent('test-model', api_key='test', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=output)))
        with pytest.raises(GateError):
            agent.run({}, Risk)


def test_repository_tools_reject_escape_and_protected_files(tmp_path):
    tools = RepositoryTools(tmp_path, writable=True, commands={})
    for path in ('../outside', '.git/config', 'brain/policy.md', '.env', 'a/.env.local', 'C:/Windows/a'):
        with pytest.raises(GateError):
            tools.call('write_file', {'path': path, 'content': 'bad'})
    tools.call('write_file', {'path': 'src/app.py', 'content': 'value = 1'})
    assert tools.call('read_file', {'path': 'src/app.py'}) == 'value = 1'


def test_review_tools_cannot_write_or_execute_commands(tmp_path):
    tools = RepositoryTools(tmp_path, writable=False, commands={})
    with pytest.raises(GateError):
        tools.call('write_file', {'path': 'app.py', 'content': 'bad'})
    with pytest.raises(GateError):
        tools.call('run_check', {'name': 'tests', 'task_id': '', 'phase': 'check'})


def test_unconfigured_ui_never_returns_pass():
    from factory.worker import require_browser
    with pytest.raises(GateError, match='browser'):
        require_browser(None)


def test_worker_executes_real_tdd_and_resumes_each_stage(tmp_path):
    from factory.contracts import Spec, Review
    from factory.worker import Worker, Config, BuildResult, worker_lock
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'brain').mkdir()
    (repo / 'brain' / 'architecture.md').write_text('Use Python. Require regression tests.')
    (repo / 'app.py').write_text('value = 0\n')
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'initial')
    base = git('rev-parse', 'HEAD')
    calls = []
    class Agent:
        def __init__(self, model): pass
        def run(self, context, contract, tools=None):
            role = context['role']
            calls.append(role)
            if role == 'spec':
                return Spec(objective='Fix value', acceptance=['Value is one'], exclusions=[], rollback='Revert',
                    brain_revision=base, tasks=[{'id': 'value', 'description': 'Fix value',
                        'acceptance': ['Value is one'], 'validation': 'tdd'}])
            if role == 'builder':
                tools.call('write_file', {'path': 'check.py', 'content': 'from app import value\nassert value == 1\n'})
                red = tools.call('run_check', {'name': 'tests', 'task_id': 'value', 'phase': 'red'})
                assert red['assertion_failure']
                tools.call('write_file', {'path': 'app.py', 'content': 'value = 1\n'})
                green = tools.call('run_check', {'name': 'tests', 'task_id': 'value', 'phase': 'green'})
                assert green['exit_code'] == 0
                return BuildResult(summary='Fixed value')
            if role == 'risk':
                return Risk(commit=context['commit'], level='medium', reasons=['Behavior change'], reversible=True, uncertain=False)
            return Review(role=role, commit=context['commit'], verdict='PASS', summary='Test review', evidence=['test:report'])
    class Browser:
        def review(self, agent, context, path):
            return agent.run(context, Review)
    config = Config(repo=str(repo), database=str(tmp_path / 'factory.db'), worktrees=str(tmp_path / 'trees'),
        artifacts=str(tmp_path / 'artifacts'), models={'default': 'fake'}, checks={
            'tests': [sys.executable, '-B', 'check.py'],
            'lint': [sys.executable, '-c', 'print("lint fixture")'],
            'acceptance': [sys.executable, '-B', 'check.py']})
    worker = Worker(config, agent_factory=Agent, browser_factory=Browser())
    task_id = worker.factory.submit('prompt', 'one', str(repo), 'Fix value')['id']
    for expected in ('BUILD_PENDING', 'VERIFY_PENDING', 'REVIEW_PENDING', 'RISK_PENDING', 'HUMAN_REQUIRED'):
        worker = Worker(config, agent_factory=Agent, browser_factory=Browser())
        assert worker.step(task_id)['state'] == expected
    assert calls == ['spec', 'builder', 'security', 'ux', 'ui', 'consistency', 'risk']
    with worker_lock(config.database):
        with pytest.raises(GateError, match='Worker already'):
            worker.step(task_id)
