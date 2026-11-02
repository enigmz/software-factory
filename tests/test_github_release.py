import pytest

from factory.contracts import Build, Review, Risk, Spec, Verification
from factory.engine import GateError, ROLES
from factory.github import GitHubPublisher
from factory.worker import Config

SHA, BASE, MERGE = 'a' * 40, 'b' * 40, 'c' * 40


class Remote:
    def __init__(self):
        self.merges = 0
        self.merged = False
        self.passed = True
        self.fail_after_merge = False

    def status(self, *args):
        return dict(commit=SHA, url='https://github.com/enigmz/software-factory/pull/2',
                    state='closed' if self.merged else 'open', merged=self.merged,
                    merge_commit=MERGE if self.merged else None, checks_passed=self.passed,
                    required_checks_configured=True)

    def merge(self, number, branch, commit, base, paths):
        assert commit == SHA and base == BASE and paths == ['README.md']
        self.merges += 1
        self.merged = True
        if self.fail_after_merge:
            raise GateError('Outcome unknown')


@pytest.fixture
def release(tmp_path):
    config = Config(repo=str(tmp_path), database=str(tmp_path / 'state.db'), models={}, checks={},
                    auto_merge=True, auto_paths=['README.md'],
                    github={'repository': 'enigmz/software-factory',
                            'required_checks': [{'name': 'tests', 'app_id': 15368}]})
    remote = Remote()
    publisher = GitHubPublisher(config, remote)
    f = publisher.factory
    task = f.submit('prompt', 'release-test', str(tmp_path), 'Update README')['id']
    f.set_spec(task, Spec(objective='Update docs', acceptance=['Correct docs'], exclusions=[],
        rollback='Revert', brain_revision=BASE, tasks=[{'id':'docs', 'description':'Update docs',
        'acceptance':['Correct docs'], 'validation':'visual'}]))
    f.set_build(task, Build(commit=SHA, base_commit=BASE, brain_revision=BASE,
                           changed_files=['README.md'], summary='Docs'))
    f.set_verification(task, Verification(commit=SHA, checks={'tests':True,'lint':True,'acceptance':True},
        evidence=['fixture'], task_evidence={'docs':['visual:fixture']}))
    for role in ROLES:
        f.review(task, Review(role=role, commit=SHA, verdict='PASS', summary='Fixture', evidence=['fixture']))
    f.approve(task)
    f.classify(task, Risk(commit=SHA, level='low', reasons=['Docs'], reversible=True, uncertain=False))
    def attach(t):
        t['data']['github_pr'] = dict(number=2, branch='codex/test', commit=SHA,
                                     url='https://github.com/enigmz/software-factory/pull/2')
    f._change(task, ['RELEASE_READY'], 'pr_published', {}, attach)
    return publisher, remote, task


def test_merge_and_reconciliation_are_idempotent(release):
    publisher, remote, task = release
    assert publisher.advance(task)['state'] == 'MERGED'
    assert publisher.advance(task)['state'] == 'MERGED'
    assert remote.merges == 1
    assert len([e for e in publisher.factory.events(task) if e['kind'] == 'merged']) == 1


def test_unknown_outcome_recovers_without_second_merge(release):
    publisher, remote, task = release
    remote.fail_after_merge = True
    with pytest.raises(GateError, match='unknown'):
        publisher.advance(task)
    assert publisher.advance(task)['state'] == 'MERGED'
    assert remote.merges == 1


def test_pending_ci_waits_without_merging(release):
    publisher, remote, task = release
    remote.passed = False
    assert publisher.advance(task)['state'] == 'RELEASE_READY'
    assert remote.merges == 0


def test_policy_revocation_blocks_previous_ready_task(release):
    publisher, remote, task = release
    publisher.config.auto_merge = False
    with pytest.raises(GateError, match='authorization'):
        publisher.advance(task)
    assert remote.merges == 0


def test_external_human_merge_is_reconciled_without_fabricated_approval(release):
    publisher, remote, task = release
    def human(t):
        t['state'] = 'HUMAN_REQUIRED'
    publisher.factory._change(task, ['RELEASE_READY'], 'fixture', {}, human)
    assert publisher.advance(task)['state'] == 'HUMAN_REQUIRED'
    remote.merged = True
    assert publisher.advance(task)['state'] == 'MERGED'
    assert 'human_approval' not in publisher.factory.get(task)['data']
    assert remote.merges == 0


@pytest.mark.parametrize('field', ['reviews', 'verification', 'risk'])
def test_stale_evidence_blocks_merge(release, field):
    publisher, remote, task = release
    def stale(t):
        target = t['data'][field]['security'] if field == 'reviews' else t['data'][field]
        target['commit'] = BASE
    publisher.factory._change(task, ['RELEASE_READY'], 'fixture', {}, stale)
    with pytest.raises(GateError):
        publisher.advance(task)
    assert remote.merges == 0


def client_fixture():
    from factory.github import GitHubClient, GitHubConfig
    client = GitHubClient(GitHubConfig(repository='enigmz/software-factory',
        required_checks=[{'name':'tests', 'app_id':15368}]))
    pr = dict(number=2, html_url='https://github.com/enigmz/software-factory/pull/2',
              head={'sha':SHA, 'ref':'codex/test', 'repo':{'full_name':'enigmz/software-factory'}},
              base={'sha':BASE, 'ref':'main', 'repo':{'full_name':'enigmz/software-factory'}},
              state='open', draft=False, merged=False)
    protection = {'required_status_checks': {'strict':True, 'checks':[{'context':'tests','app_id':15368}]},
                  'enforce_admins':{'enabled':True}}
    writes = []
    def api(path, payload=None, **kwargs):
        if payload:
            writes.append((path, payload, kwargs))
            return {'merged':True}
        return protection if path.endswith('/protection') else pr
    client.api = api
    client.pages = lambda *args: [{'filename':'README.md'}]
    client.status = lambda *args: {'merged':False, 'state':'open', 'checks_passed':True}
    return client, pr, protection, writes


def test_merge_request_is_bound_to_exact_sha():
    client, _, _, writes = client_fixture()
    client.merge(2, 'codex/test', SHA, BASE, ['README.md'])
    assert writes == [('pulls/2/merge', {'sha':SHA,'merge_method':'merge'}, {'method':'PUT'})]


@pytest.mark.parametrize('failure', ['base', 'head', 'app', 'admin', 'strict', 'paths', 'ci'])
def test_remote_guard_failure_never_sends_merge(failure):
    client, pr, protection, writes = client_fixture()
    if failure == 'base': pr['base']['sha'] = MERGE
    if failure == 'head': pr['head']['sha'] = MERGE
    if failure == 'app': protection['required_status_checks']['checks'][0]['app_id'] = 1
    if failure == 'admin': protection['enforce_admins']['enabled'] = False
    if failure == 'strict': protection['required_status_checks']['strict'] = False
    if failure == 'paths': client.pages = lambda *args: [{'filename':'README.md','previous_filename':'auth.py'}]
    if failure == 'ci': client.status = lambda *args: {'merged':False,'state':'open','checks_passed':False}
    with pytest.raises(GateError):
        client.merge(2, 'codex/test', SHA, BASE, ['README.md'])
    assert not writes


def test_human_approval_can_authorize_with_automatic_merge_disabled(release):
    publisher, remote, task = release
    publisher.config.auto_merge = False
    def human(t):
        t['state'] = 'HUMAN_REQUIRED'
    publisher.factory._change(task, ['RELEASE_READY'], 'fixture', {}, human)
    publisher.factory.human_approve(task, SHA, 'operator')
    assert publisher.advance(task)['state'] == 'MERGED'
    assert remote.merges == 1


def test_closed_unmerged_pr_blocks_and_missing_receipt_cannot_complete(release):
    publisher, remote, task = release
    original = remote.status
    remote.status = lambda *args: {**original(), 'state':'closed'}
    with pytest.raises(GateError, match='closed'):
        publisher.advance(task)
    assert remote.merges == 0
    remote.merged = True
    remote.status = lambda *args: {**original(), 'merge_commit':None}
    with pytest.raises(GateError, match='merge commit'):
        publisher.advance(task)
    assert publisher.factory.get(task)['state'] == 'RELEASE_READY'


def test_database_failure_after_remote_merge_is_recovered(release, monkeypatch):
    publisher, remote, task = release
    original = publisher.factory._change
    def interrupted(task_id, states, kind, payload, update):
        if kind == 'merged':
            raise OSError('Database unavailable')
        return original(task_id, states, kind, payload, update)
    monkeypatch.setattr(publisher.factory, '_change', interrupted)
    with pytest.raises(OSError):
        publisher.advance(task)
    monkeypatch.setattr(publisher.factory, '_change', original)
    assert publisher.advance(task)['state'] == 'MERGED'
    assert remote.merges == 1
    events = publisher.factory.events(task)
    assert len([e for e in events if e['kind'] == 'merge_requested']) == 1
    assert len([e for e in events if e['kind'] == 'merged']) == 1


def test_completed_release_never_restarts_agents(release):
    from factory.worker import Worker
    publisher, _, task = release
    publisher.advance(task)
    worker = Worker(publisher.config, agent_factory=lambda _: pytest.fail('No models after merge'))
    assert worker.step(task)['state'] == 'MERGED'
