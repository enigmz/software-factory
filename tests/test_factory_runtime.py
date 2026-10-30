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


def test_api_error_exposes_code_without_echoing_response_message():
    agent = OpenAIAgent('test-model', api_key='test-secret', transport=httpx.MockTransport(
        lambda request: httpx.Response(429, json={'error': {'code': 'insufficient_quota',
            'message': 'Do not print test-secret or prompt content'}})))
    with pytest.raises(GateError, match='HTTP 429 .*insufficient_quota') as error:
        agent.run({}, Risk)
    assert 'test-secret' not in str(error.value)


def test_multimodal_tool_output_is_explicit_and_lists_remain_json():
    from factory.tool_output import ToolContent
    for value, expected_type in ((['a.py', 'b.py'], str),
        (ToolContent([{'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='}]), list)):
        requests = []
        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                return httpx.Response(200, json={'status': 'completed', 'output': [
                    {'type': 'function_call', 'name': 'observe', 'arguments': '{}', 'call_id': 'call_1'}]})
            assert isinstance(payload['input'][-1]['output'], expected_type)
            return httpx.Response(200, json={'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                    'commit': 'a' * 40, 'level': 'low', 'reasons': ['Observed'],
                    'reversible': True, 'uncertain': False})}]}]})
        class Tools:
            def definitions(self): return []
            def call(self, name, args): return value
        agent = OpenAIAgent('test-model', api_key='test', transport=httpx.MockTransport(respond))
        agent.run({}, Risk, Tools())
        assert len(requests) == 2


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


@pytest.mark.parametrize('visual_pass', [None, False, True])
def test_worker_executes_real_tdd_and_resumes_each_stage(tmp_path, visual_pass):
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
                        'acceptance': ['Value is one'], 'validation': 'tdd' if visual_pass is None else 'visual'}])
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
        def verify_task(self, commit, task):
            from factory.browser import VisualResult
            return VisualResult(visual_pass, ['visual:fixture'], 'Observed fixture')
        def review(self, agent, context, path):
            return agent.run(context, Review)
    config = Config(repo=str(repo), database=str(tmp_path / 'factory.db'), worktrees=str(tmp_path / 'trees'),
        artifacts=str(tmp_path / 'artifacts'), isolation={'mode': 'local'}, models={'default': 'fake'}, checks={
            'tests': [sys.executable, '-B', 'check.py'],
            'lint': [sys.executable, '-c', 'print("lint fixture")'],
            'acceptance': [sys.executable, '-B', 'check.py']})
    worker = Worker(config, agent_factory=Agent, browser_factory=Browser())
    task_id = worker.factory.submit('prompt', 'one', str(repo), 'Fix value')['id']
    expected_states = ('BUILD_PENDING', 'VERIFY_PENDING', 'REVIEW_PENDING', 'RISK_PENDING', 'HUMAN_REQUIRED')
    if visual_pass is False:
        expected_states = ('BUILD_PENDING', 'VERIFY_PENDING', 'BUILD_PENDING')
    for expected in expected_states:
        worker = Worker(config, agent_factory=Agent, browser_factory=Browser())
        assert worker.step(task_id)['state'] == expected
    if visual_pass is False:
        assert calls == ['spec', 'builder']
        assert worker.factory.get(task_id)['data']['feedback']['verification']['checks']['visual:value'] is False
    else:
        assert calls == ['spec', 'builder', 'security', 'ux', 'ui', 'consistency', 'risk']
    with worker_lock(config.database):
        with pytest.raises(GateError, match='Worker already'):
            worker.step(task_id)
