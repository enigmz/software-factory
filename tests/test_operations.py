import pytest
from factory.engine import Factory, GateError
from factory.operations import Budget, Operations


def setup(tmp_path, **limits):
    path = tmp_path / 'state.sqlite3'
    task = Factory(path).submit('prompt', 'one', 'repo', 'test')['id']
    budget = Budget(prices={'model': {'input_usd_per_million': 2.0, 'output_usd_per_million': 8.0}}, **limits)
    return Operations(path, budget), task


def test_budget_survives_restart_and_reserves_atomically(tmp_path):
    ops, task = setup(tmp_path, max_tokens=100)
    request = ops.reserve(task, 'builder', 'model', 30, 50)
    with pytest.raises(GateError, match='budget'):
        Operations(ops.path, ops.budget).reserve(task, 'ui', 'model', 30, 50)
    ops.settle(request, {'input_tokens': 30, 'output_tokens': 10})
    assert ops.usage(task)['tokens'] == 40
    assert ops.usage(task)['cost_micro_usd'] == 140


def test_unknown_call_keeps_reservation_on_recovery(tmp_path):
    ops, task = setup(tmp_path)
    ops.reserve(task, 'builder', 'model', 30, 50)
    ops.recover(task, 'Operator inspected interrupted work')
    usage = ops.usage(task)
    assert usage['tokens'] == 80
    assert usage['calls'][0]['status'] == 'unknown'


def test_interrupted_stage_requires_explicit_recovery(tmp_path):
    ops, task = setup(tmp_path)
    ops.begin(task, 'BUILD_PENDING', 0)
    with pytest.raises(GateError, match='recover'):
        Operations(ops.path, ops.budget).begin(task, 'BUILD_PENDING', 0)
    ops.recover(task, 'Inspected worktree')
    ops.begin(task, 'BUILD_PENDING', 0)


def test_missing_price_and_malformed_usage_fail_closed(tmp_path):
    ops, task = setup(tmp_path)
    with pytest.raises(GateError, match='price'):
        ops.reserve(task, 'spec', 'unknown', 10, 10)
    request = ops.reserve(task, 'spec', 'model', 10, 10)
    with pytest.raises(GateError, match='usage'):
        ops.settle(request, {'input_tokens': -1, 'output_tokens': 0})
    assert ops.usage(task)['tokens'] == 20


def test_concurrent_reservations_cannot_overspend(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    ops, task = setup(tmp_path, max_tokens=100)
    def reserve(_):
        try:
            return ops.reserve(task, 'builder', 'model', 40, 40)
        except GateError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(value is not None for value in pool.map(reserve, range(4))) == 1


def test_adapter_counts_and_blocks_before_generation(tmp_path):
    import httpx
    from factory.openai_agents import OpenAIAgent
    from factory.contracts import Risk
    ops, task = setup(tmp_path, max_tokens=10)
    seen = []
    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={'input_tokens': 20})
    agent = OpenAIAgent('model', api_key='synthetic', transport=httpx.MockTransport(respond),
                        operations=ops, task_id=task, role='risk')
    with pytest.raises(GateError, match='budget'):
        agent.run({}, Risk)
    assert seen == ['/v1/responses/input_tokens']


def test_adapter_accounts_incomplete_and_unknown_responses(tmp_path):
    import httpx
    from factory.openai_agents import OpenAIAgent
    from factory.contracts import Risk
    ops, task = setup(tmp_path)
    def respond(request):
        if request.url.path.endswith('input_tokens'):
            return httpx.Response(200, json={'input_tokens': 20})
        return httpx.Response(200, json={'status': 'incomplete', 'usage': {'input_tokens': 20, 'output_tokens': 3}})
    agent = OpenAIAgent('model', api_key='synthetic', transport=httpx.MockTransport(respond),
                        operations=ops, task_id=task, role='risk')
    with pytest.raises(GateError, match='incomplete'):
        agent.run({}, Risk)
    assert ops.usage(task)['tokens'] == 23
    def timeout(request):
        if request.url.path.endswith('input_tokens'):
            return httpx.Response(200, json={'input_tokens': 20})
        raise httpx.ReadTimeout('synthetic timeout')
    agent.transport = httpx.MockTransport(timeout)
    with pytest.raises(GateError, match='ReadTimeout'):
        agent.run({}, Risk)
    assert ops.usage(task)['tokens'] == 23 + 20 + ops.budget.max_output_tokens


def test_os_lock_releases_after_process_crash(tmp_path):
    import subprocess
    import sys
    from factory.worker import worker_lock
    path = str(tmp_path / 'state.db')
    script = 'from factory.worker import worker_lock; import os,sys\nwith worker_lock(sys.argv[1]): os._exit(17)'
    result = subprocess.run([sys.executable, '-c', script, path], timeout=10)
    assert result.returncode == 17
    with worker_lock(path):
        pass


@pytest.mark.parametrize('limits', [{'max_calls': 1}, {'max_cost_usd': 0.00015}])
def test_calls_and_money_limits_persist(tmp_path, limits):
    ops, task = setup(tmp_path, **limits)
    key = ops.reserve(task, 'spec', 'model', 10, 10)
    ops.settle(key, {'input_tokens': 10, 'output_tokens': 10})
    with pytest.raises(GateError, match='budget'):
        Operations(ops.path, ops.budget).reserve(task, 'builder', 'model', 10, 10)


def test_overrun_is_recorded_before_blocking_and_settlement_is_idempotent(tmp_path):
    ops, task = setup(tmp_path)
    key = ops.reserve(task, 'spec', 'model', 10, 10)
    usage = {'input_tokens': 20, 'output_tokens': 20}
    with pytest.raises(GateError, match='exceeded reservation'):
        ops.settle(key, usage)
    ops.settle(key, usage)
    assert ops.usage(task)['tokens'] == 40
    assert len(ops.usage(task)['calls']) == 1
