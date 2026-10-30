import pytest
from factory.engine import GateError
from factory.github import GitHubClient, GitHubConfig, validate_pr, evaluate_checks


def pr(sha='a' * 40):
    return {'number': 1, 'html_url': 'https://github.com/enigmz/software-factory/pull/1',
            'state': 'open', 'draft': True,
            'head': {'sha': sha, 'ref': 'codex/test', 'repo': {'full_name': 'enigmz/software-factory'}},
            'base': {'ref': 'main', 'repo': {'full_name': 'enigmz/software-factory'}}}


def test_pr_is_bound_to_repo_branch_and_commit():
    config = GitHubConfig(repository='enigmz/software-factory')
    validate_pr(pr(), config, 'codex/test', 'a' * 40)
    for mutation in ('commit', 'repo', 'base'):
        value = pr()
        if mutation == 'commit': value['head']['sha'] = 'b' * 40
        if mutation == 'repo': value['head']['repo']['full_name'] = 'other/repo'
        if mutation == 'base': value['base']['ref'] = 'other'
        with pytest.raises(GateError):
            validate_pr(value, config, 'codex/test', 'a' * 40)


def test_missing_pending_failed_or_wrong_app_checks_never_pass():
    required = [{'name': 'tests', 'app_id': 123}]
    good = {'name': 'tests', 'head_sha': 'a' * 40, 'app': {'id': 123}, 'status': 'completed', 'conclusion': 'success'}
    assert evaluate_checks([good], [], required, 'a' * 40)
    assert not evaluate_checks([], [], required, 'a' * 40)
    assert not evaluate_checks([good], [], [], 'a' * 40)
    for key, value in [('status', 'queued'), ('conclusion', 'failure'), ('head_sha', 'b' * 40), ('app', {'id': 456})]:
        assert not evaluate_checks([{**good, key: value}], [], required, 'a' * 40)
    assert not evaluate_checks([good], [{'state': 'failure'}], required, 'a' * 40)


def test_existing_pr_is_reused_without_post():
    client = GitHubClient(GitHubConfig(repository='enigmz/software-factory'))
    client.pages = lambda *args, **kwargs: [pr()]
    client.api = lambda *args, **kwargs: pytest.fail('Existing PR must not be posted again')
    assert client.ensure_draft('codex/test', 'a' * 40, 'Title', 'Body')['number'] == 1


def test_create_uses_draft_and_exact_head():
    client = GitHubClient(GitHubConfig(repository='enigmz/software-factory'))
    client.pages = lambda *args, **kwargs: []
    sent = []
    def api(path, payload):
        sent.append((path, payload))
        return pr()
    client.api = api
    client.ensure_draft('codex/test', 'a' * 40, 'Title', 'Body')
    assert sent == [('pulls', {'head': 'codex/test', 'base': 'main', 'title': 'Title', 'body': 'Body', 'draft': True})]


def test_closed_pr_does_not_create_duplicate():
    client = GitHubClient(GitHubConfig(repository='enigmz/software-factory'))
    client.pages = lambda *args, **kwargs: [{**pr(), 'state': 'closed'}]
    with pytest.raises(GateError, match='closed'):
        client.ensure_draft('codex/test', 'a' * 40, 'Title', 'Body')


def test_task_cannot_publish_before_review_or_enable_auto_merge(tmp_path):
    from factory.worker import Config
    from factory.github import GitHubPublisher
    from factory.engine import Factory
    config = Config(repo=str(tmp_path), database=str(tmp_path / 'state.db'), models={}, checks={},
                    github={'repository': 'enigmz/software-factory'})
    task = Factory(config.database).submit('prompt', 'one', str(tmp_path), 'test')
    publisher = GitHubPublisher(config)
    publisher.client.api = lambda *args, **kwargs: pytest.fail('No network before approval gates')
    with pytest.raises(GateError, match='four reviews'):
        publisher.publish(task['id'])
    with pytest.raises(ValueError, match='human merge'):
        Config(repo=str(tmp_path), models={}, checks={}, auto_merge=True,
               github={'repository': 'enigmz/software-factory'})


def test_remote_head_change_during_status_is_rejected():
    client = GitHubClient(GitHubConfig(repository='enigmz/software-factory'))
    results = iter([pr(), pr('b' * 40)])
    client.api = lambda *args: next(results)
    client.pages = lambda *args: []
    with pytest.raises(GateError, match='commit'):
        client.status(1, 'codex/test', 'a' * 40)


def test_publish_persists_pr_and_reuses_exact_commit(tmp_path):
    import subprocess
    from factory.worker import Config
    from factory.github import GitHubPublisher
    from factory.engine import Factory, ROLES
    from factory.workspace import Workspace
    from factory.contracts import Spec, Build, Verification, Review, Risk
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    (repo / 'landing.css').write_text('body {color:black}')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'base')
    base = git('rev-parse', 'HEAD')
    config = Config(repo=str(repo), database=str(tmp_path / 'state.db'), worktrees=str(tmp_path / 'trees'),
                    artifacts=str(tmp_path / 'artifacts'), models={}, checks={}, github={'repository':'enigmz/software-factory'})
    f = Factory(config.database)
    task = f.submit('prompt', 'one', str(repo), 'SECRET PROMPT must never be published')['id']
    f.set_spec(task, Spec(objective='Color', acceptance=['Color'], exclusions=[], rollback='Revert', brain_revision=base,
                         tasks=[{'id':'color','description':'Color','acceptance':['Color'],'validation':'visual'}]))
    workspace = Workspace(repo, config.worktrees)
    tree = workspace.prepare(task, base)['path']
    from pathlib import Path
    Path(tree, 'landing.css').write_text('body {color:blue}')
    workspace.git('add', '.', cwd=tree)
    workspace.git('-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-m','color',cwd=tree)
    snapshot = workspace.snapshot(tree, base)
    commit = snapshot['commit']
    f.set_build(task, Build(**snapshot,base_commit=base,brain_revision=base,summary='Color'))
    f.set_verification(task, Verification(commit=commit,checks={'tests':True,'lint':True,'acceptance':True},evidence=['fixture'],task_evidence={'color':['visual:fixture']}))
    for role in ROLES:
        f.review(task, Review(role=role,commit=commit,verdict='PASS',summary='Fixture',evidence=['fixture']))
    f.approve(task)
    f.classify(task,Risk(commit=commit,level='low',reasons=['Fixture'],reversible=True,uncertain=False))
    client = GitHubClient(config.github)
    remote = []
    client.pages = lambda *args: remote
    def api(path,payload):
        assert 'SECRET PROMPT' not in payload['body']
        value = pr(commit)
        value['head']['ref'] = payload['head']
        remote.append(value)
        return value
    client.api = api
    publisher = GitHubPublisher(config,client)
    original = publisher.workspace.git
    pushes = []
    def remote_git(*args,**kwargs):
        if args[0] == 'remote': return 'https://github.com/enigmz/software-factory.git'
        if args[0] == 'ls-remote': return (commit + '\t' + args[-1]) if pushes else ''
        if 'push' in args:
            pushes.append(args)
            assert args[-1].startswith(commit + ':refs/heads/codex/factory-')
            assert '--force' not in args
            return ''
        return original(*args,**kwargs)
    publisher.workspace.git = remote_git
    first = publisher.publish(task)
    assert publisher.publish(task) == first
    assert len(pushes) == len(remote) == 1
    assert f.get(task)['state'] == 'HUMAN_REQUIRED'
    assert f.get(task)['data']['github_pr']['commit'] == commit
    assert len([e for e in f.events(task) if e['kind']=='pr_published']) == 1
