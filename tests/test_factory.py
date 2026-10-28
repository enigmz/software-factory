import concurrent.futures
import json
import subprocess

import pytest

from factory.engine import Factory, GateError
from factory.contracts import Review, Spec, Build, Verification, Risk
from factory.workspace import Workspace
from factory.release import ReleaseController


SHA = 'a' * 40
OTHER = 'b' * 40


@pytest.fixture
def factory(tmp_path):
    return Factory(tmp_path / 'factory.sqlite3')


def spec():
    return Spec(objective='Change landing background', acceptance=['Background matches design'],
                exclusions=['Shared tokens'], tasks=[{'id': 'background', 'description': 'Update local CSS',
                'acceptance': ['Background matches design'], 'validation': 'visual'}],
                rollback='Revert PR', brain_revision=SHA)


def built(factory):
    task = factory.submit('prompt', 'event-1', 'repo', 'Change background')
    factory.set_spec(task['id'], spec())
    factory.set_build(task['id'], Build(commit=SHA, base_commit=OTHER, brain_revision=SHA,
                      changed_files=['landing.css'], summary='Background changed'))
    return task['id']


def verified(factory):
    task_id = built(factory)
    factory.set_verification(task_id, Verification(commit=SHA, checks={'tests': True, 'lint': True,
        'acceptance': True}, evidence=['runner:checks/1'], task_evidence={'background': ['visual:1']}))
    return task_id


def reviewed(factory):
    task_id = verified(factory)
    for role in ('security', 'ux', 'ui', 'consistency'):
        factory.review(task_id, Review(role=role, commit=SHA, verdict='PASS',
            summary='Checked scoped criteria', evidence=['report:1']))
    factory.approve(task_id)
    return task_id


def test_dedup_is_atomic_and_survives_restart(factory):
    def submit(_):
        return factory.submit('sentry', 'event-1', 'repo', 'Error')['id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(8)))
    assert len(set(ids)) == 1
    assert Factory(factory.path).get(ids[0])['state'] == 'SPEC_PENDING'
    with pytest.raises(GateError, match='different'):
        factory.submit('sentry', 'event-1', 'repo', 'Different error')


def test_no_stage_skipping(factory):
    task = factory.submit('prompt', 'x', 'repo', 'Change')
    with pytest.raises(GateError):
        factory.approve(task['id'])


def test_four_reviews_required_and_duplicates_do_not_count(factory):
    task_id = verified(factory)
    r = Review(role='security', commit=SHA, verdict='PASS', summary='Checked', evidence=['report'])
    factory.review(task_id, r)
    factory.review(task_id, r)
    with pytest.raises(GateError):
        factory.approve(task_id)


def test_stale_review_rejected(factory):
    task_id = verified(factory)
    with pytest.raises(GateError, match='commit'):
        factory.review(task_id, Review(role='ui', commit=OTHER, verdict='PASS', summary='Checked', evidence=['x']))


def test_failed_review_returns_to_builder_and_invalidates_evidence(factory):
    task_id = verified(factory)
    for role in ('security', 'ux', 'ui', 'consistency'):
        factory.review(task_id, Review(role=role, commit=SHA, verdict='FAIL' if role == 'ui' else 'PASS',
            summary='Contrast problem' if role == 'ui' else 'Checked', evidence=['report']))
    factory.approve(task_id)
    task = factory.get(task_id)
    assert task['state'] == 'BUILD_PENDING'
    assert task['data']['feedback']['ui']['verdict'] == 'FAIL'
    assert 'verification' not in task['data']
    assert task['data']['reviews'] == {}


def test_failed_verification_cannot_reach_review(factory):
    task_id = built(factory)
    factory.set_verification(task_id, Verification(commit=SHA, checks={'tests': False, 'lint': True,
        'acceptance': True}, evidence=['runner:1'], task_evidence={'background': ['visual:1']}))
    assert factory.get(task_id)['state'] == 'BUILD_PENDING'


def test_tdd_requires_red_and_green_for_each_behavior(factory):
    task = factory.submit('prompt', 'x', 'repo', 'Fix behavior')
    s = spec()
    s.tasks[0].validation = 'tdd'
    factory.set_spec(task['id'], s)
    factory.set_build(task['id'], Build(commit=SHA, base_commit=OTHER, brain_revision=SHA,
        changed_files=['app.py'], summary='Fix'))
    with pytest.raises(GateError, match='TDD'):
        factory.set_verification(task['id'], Verification(commit=SHA, checks={'tests': True, 'lint': True,
            'acceptance': True}, evidence=['log'], task_evidence={'background': ['green:1']}))


def test_low_risk_requires_explicit_allowlist_and_enabled_policy(factory):
    task_id = reviewed(factory)
    factory.classify(task_id, Risk(commit=SHA, level='low', reasons=['Local cosmetic change'],
                                  reversible=True, uncertain=False))
    assert factory.get(task_id)['state'] == 'HUMAN_REQUIRED'
    with pytest.raises(GateError):
        factory.human_approve(task_id, OTHER, 'Isra')
    factory.human_approve(task_id, SHA, 'Isra')
    assert factory.get(task_id)['state'] == 'RELEASE_READY'


def test_sensitive_files_override_low_risk_agent(tmp_path):
    f = Factory(tmp_path / 'f.db', auto_merge=True, auto_paths=['**'])
    task_id = built(f)
    # The independent policy must refuse suspicious paths even with a broad allowlist.
    assert not f.policy_allows(['src/auth/login.py'])
    assert not f.policy_allows(['.github/workflows/build.yml'])
    assert not f.policy_allows(['../landing.css'])
    assert not f.policy_allows(['backend/payments.py'])


def test_clean_reviewer_context_contains_no_builder_conversation(factory):
    task_id = verified(factory)
    context = factory.context(task_id, 'ui', {'revision': SHA, 'documents': {'design.md': 'Use tokens'}})
    assert context['role'] == 'ui'
    assert context['commit'] == SHA
    assert 'summary' not in context
    assert 'reviews' not in context
    assert 'conversation' not in context
    assert context['requirements']['computer_use'] is True


def test_retry_budget_escalates(factory):
    task_id = built(factory)
    for attempt in range(3):
        factory.set_verification(task_id, Verification(commit=SHA, checks={'tests': False, 'lint': True,
            'acceptance': True}, evidence=['log'], task_evidence={'background': ['visual:1']}))
        if attempt < 2:
            factory.set_build(task_id, Build(commit=SHA, base_commit=OTHER, brain_revision=SHA,
                changed_files=['landing.css'], summary='Retry'))
    assert factory.get(task_id)['state'] == 'ESCALATED'


def test_event_log_records_committed_transitions(factory):
    task_id = reviewed(factory)
    events = factory.events(task_id)
    assert events[0]['kind'] == 'submitted'
    assert events[-1]['kind'] == 'approved'
    assert factory.get(task_id)['state'] == 'RISK_PENDING'


def test_worktree_uses_real_commit_and_is_idempotent(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.email', 'factory@example.test')
    git('config', 'user.name', 'Factory test')
    (repo / 'readme.txt').write_text('test')
    git('add', '.')
    git('commit', '-m', 'initial')
    workspace = Workspace(repo, tmp_path / 'worktrees')
    result = workspace.prepare('0123456789abcdef', git('rev-parse', 'HEAD'))
    assert result['branch'] == 'codex/factory-0123456789abcdef'
    assert workspace.prepare('0123456789abcdef', result['base_commit']) == result


def test_release_requires_verified_merge_and_deployment_observation(factory):
    task_id = reviewed(factory)
    factory.classify(task_id, Risk(commit=SHA, level='high', reasons=['Broad change'], reversible=True, uncertain=False))
    release = ReleaseController(factory)
    with pytest.raises(GateError):
        release.merged(task_id, reviewed_commit=SHA, merge_commit=OTHER, pr_url='https://example.test/pr/1', checks_passed=True)
    factory.human_approve(task_id, SHA, 'Isra')
    with pytest.raises(GateError):
        release.merged(task_id, reviewed_commit=OTHER, merge_commit=OTHER, pr_url='https://example.test/pr/1', checks_passed=True)
    release.merged(task_id, reviewed_commit=SHA, merge_commit=OTHER, pr_url='https://example.test/pr/1', checks_passed=True)
    with pytest.raises(GateError):
        release.deployed(task_id, merge_commit=SHA, deployment_id='deploy-1')
    release.deployed(task_id, merge_commit=OTHER, deployment_id='deploy-1')
    with pytest.raises(GateError):
        release.observed(task_id, deployment_id='deploy-1', healthy=True, window_complete=False, evidence=['health'])
    release.observed(task_id, deployment_id='deploy-1', healthy=False, window_complete=False, evidence=['regression'])
    assert factory.get(task_id)['state'] == 'ROLLBACK_REQUIRED'
    release.rolled_back(task_id, deployment_id='deploy-1', evidence=['rollback receipt'])
    assert factory.get(task_id)['state'] == 'ROLLED_BACK'


def test_auto_release_requires_explicit_policy(tmp_path):
    f = Factory(tmp_path / 'f.db', auto_merge=True, auto_paths=['landing.css'])
    task_id = reviewed(f)
    f.classify(task_id, Risk(commit=SHA, level='low', reasons=['Local CSS'], reversible=True, uncertain=False))
    assert f.get(task_id)['state'] == 'RELEASE_READY'


def test_blocked_and_not_applicable_are_never_passes(factory):
    task_id = verified(factory)
    for role in ('security', 'ux', 'ui', 'consistency'):
        factory.review(task_id, Review(role=role, commit=SHA, verdict='BLOCKED' if role == 'ui' else 'PASS',
            summary='Browser unavailable' if role == 'ui' else 'Checked', evidence=['report']))
    factory.approve(task_id)
    assert factory.get(task_id)['state'] == 'BUILD_PENDING'
