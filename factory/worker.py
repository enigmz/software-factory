"""Single-worker executor. Pause safely at missing integrations; resume by stage."""
import json
import os
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field, model_validator

from .contracts import Contract, Spec, Review, Risk, Build, Text, Verification
from .engine import Factory, GateError, ROLES
from .openai_agents import OpenAIAgent
from .runtime import RepositoryTools
from .workspace import Workspace
from .browser import BrowserConfig, BrowserHarness
from .preview import PreviewConfig
from .managed_browser import ManagedBrowserHarness
from .isolation import IsolationConfig, DockerExecutor
from .isolated_browser import DockerBrowserHarness
from .operations import Budget, Operations


class BuildResult(Contract):
    summary: Text


class Config(Contract):
    repo: str
    database: str = 'data/factory/state.sqlite3'
    worktrees: str = 'data/factory/worktrees'
    artifacts: str = 'data/factory/artifacts'
    models: dict[str, str]
    checks: dict[str, list[str]]
    auto_merge: bool = False
    auto_paths: list[str] = Field(default_factory=list)
    browser: BrowserConfig | None = None
    preview: PreviewConfig | None = None
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode='after')
    def preview_requires_browser(self):
        if self.preview is not None and self.browser is None:
            raise ValueError('Managed preview requires browser configuration')
        if self.isolation.mode == 'docker' and self.browser is not None and self.preview is None:
            raise ValueError('Isolated browser requires a managed static preview')
        return self


def require_browser(browser):
    if browser is None:
        raise GateError('UI review requires a configured isolated browser and commit-bound preview')
    return browser


@contextmanager
def worker_lock(database):
    path = Path(str(database) + '.worker.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = open(path, 'a+b')
    locked = False
    try:
        try:
            if os.fstat(descriptor.fileno()).st_size == 0:
                descriptor.write(b'0')
                descriptor.flush()
            descriptor.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise GateError('Worker already active') from exc
        yield
    finally:
        if locked:
            descriptor.seek(0)
            if os.name == 'nt':
                msvcrt.locking(descriptor.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        descriptor.close()


class Worker:
    def __init__(self, config: Config, *, agent_factory=OpenAIAgent, browser_factory=None):
        self.config = config
        self.factory = Factory(config.database, auto_merge=config.auto_merge, auto_paths=config.auto_paths)
        self.workspace = Workspace(config.repo, config.worktrees)
        self.agent_factory = agent_factory
        self.operations = Operations(config.database, config.budget)
        self.task_id = None
        self.executor = DockerExecutor(config.isolation, Path(config.artifacts) / 'isolation') if config.isolation.mode == 'docker' else None
        self.browser_factory = browser_factory
        if self.browser_factory is None and config.browser is not None:
            if config.isolation.mode == 'docker':
                self.browser_factory = DockerBrowserHarness(self.workspace.repo, config.preview, config.browser,
                    config.isolation, Path(config.artifacts), lambda: self._agent('ui'))
            elif config.preview is not None:
                self.browser_factory = ManagedBrowserHarness(self.workspace.repo, config.preview, config.browser,
                    Path(config.artifacts), lambda: self._agent('ui'))
            else:
                self.browser_factory = BrowserHarness(config.browser, Path(config.artifacts) / 'browser', lambda: self._agent('ui'))

    def brain(self, revision):
        files = self.workspace.git('ls-tree', '-r', '--name-only', revision, '--', 'brain').splitlines()
        documents = {p: self.workspace.git('show', revision + ':' + p) for p in files if p.endswith('.md')}
        if not documents:
            raise GateError('Repository needs a committed brain/*.md before execution')
        if sum(len(v) for v in documents.values()) > 120000:
            raise GateError('Brain exceeds context budget; scope the project documents')
        return {'revision': revision, 'documents': documents}

    def _agent(self, role):
        model = self.config.models.get(role, self.config.models.get('default'))
        if not model:
            raise GateError('No configured model for ' + role)
        agent = self.agent_factory(model)
        if isinstance(agent, OpenAIAgent):
            agent.operations, agent.task_id, agent.role = self.operations, self.task_id, role
        return agent

    def step(self, task_id):
        with worker_lock(self.config.database):
            self.task_id = task_id
            task = self.factory.get(task_id)
            if task['state'] in ('HUMAN_REQUIRED', 'ESCALATED', 'RELEASE_READY'):
                return task
            if Path(task['repo']).resolve() != self.workspace.repo:
                raise GateError('Task repository does not match configured repository')
            if self.executor is not None:
                self.executor.preflight()
            self.operations.begin(task_id, task['state'], task['data']['attempts'])
            try:
                result = self._step(task_id)
            except BaseException as exc:
                self.operations.finish(task_id, exc)
                raise
            self.operations.finish(task_id)
            return result

    def recover(self, task_id, reason):
        with worker_lock(self.config.database):
            task = self.factory.get(task_id)
            if Path(task['repo']).resolve() != self.workspace.repo:
                raise GateError('Task repository does not match configured repository')
            return self.operations.recover(task_id, reason)

    def _step(self, task_id):
        f = self.factory
        task = f.get(task_id)
        if Path(task['repo']).resolve() != self.workspace.repo:
            raise GateError('Task repository does not match configured repository')
        state = task['state']
        data = task['data']
        if state in ('HUMAN_REQUIRED', 'ESCALATED', 'RELEASE_READY'):
            return task
        if state == 'SPEC_PENDING':
            base = self.workspace.git('rev-parse', 'HEAD')
            brain = self.brain(base)
            context = f.context(task_id, 'spec', brain)
            spec_tree = self.workspace.prepare(task_id, base)
            tools = RepositoryTools(spec_tree['path'], writable=False, commands={})
            result = self._agent('spec').run(context, Spec, tools)
            if result.brain_revision != base:
                raise GateError('Spec did not use supplied Brain revision')
            return f.set_spec(task_id, result)

        base = data['spec']['brain_revision']
        brain = self.brain(base)
        worktree = self.workspace.prepare(task_id, base)
        path = worktree['path']
        artifact_dir = Path(self.config.artifacts) / task_id / str(data['attempts'])
        journal_path = artifact_dir / 'checks.json'

        if state == 'BUILD_PENDING':
            checkpoint_name = 'builder:' + str(data['attempts'])
            checkpoint = self.operations.checkpoint(task_id, checkpoint_name)
            journal = self.operations.checkpoint(task_id, checkpoint_name + ':checks') or []
            tools = RepositoryTools(path, writable=True, commands=self.config.checks,
                                    artifact_dir=artifact_dir, journal=journal, executor=self.executor,
                                    on_check=lambda records: self.operations.checkpoint(task_id, checkpoint_name + ':checks', records))
            if checkpoint:
                result = BuildResult.model_validate(checkpoint['result'])
                if self.workspace.fingerprint(path) != checkpoint['fingerprint']:
                    raise GateError('Workspace differs from builder checkpoint; operator inspection required')
            else:
                context = f.context(task_id, 'builder', brain)
                context['previous_checks'] = journal
                context['recovery_instruction'] = 'Inspect existing work before editing. Preserve valid red/green receipts; never invent or weaken tests.'
                result = self._agent('builder').run(context, BuildResult, tools)
                self.operations.checkpoint(task_id, checkpoint_name,
                    {'result': result.model_dump(), 'fingerprint': self.workspace.fingerprint(path)})
            # The controller makes the commit; it does not accept an agent-invented SHA.
            self.workspace.git('add', '--all', cwd=path)
            if self.workspace.git('diff', '--cached', '--name-only', cwd=path):
                self.workspace.git('-c', 'user.name=Software Factory', '-c', 'user.email=factory@localhost',
                    '-c', 'core.hooksPath=' + str(artifact_dir.resolve() / 'no-hooks'),
                    'commit', '-m', 'Factory task ' + task_id, cwd=path)
            snapshot = self.workspace.snapshot(path, base)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            journal_path.write_text(json.dumps(journal), encoding='utf-8')
            return f.set_build(task_id, Build(**snapshot, base_commit=base, brain_revision=base, summary=result.summary))

        snapshot = self.workspace.snapshot(path, base)
        if snapshot['commit'] != data['build']['commit'] or snapshot['changed_files'] != data['build']['changed_files']:
            raise GateError('Workspace changed since build; refusing stale evidence')

        if state == 'VERIFY_PENDING':
            if not {'tests', 'lint', 'acceptance'}.issubset(self.config.checks):
                raise GateError('Configure independent tests, lint and acceptance commands')
            tools = RepositoryTools(path, writable=False, commands=self.config.checks, artifact_dir=artifact_dir, executor=self.executor)
            checks = {name: tools.run_check(name) for name in ('tests', 'lint', 'acceptance')}
            journal = json.loads(journal_path.read_text(encoding='utf-8'))
            task_evidence = {}
            visual_checks = {}
            for unit in data['spec']['tasks']:
                records = [r for r in journal if r['task_id'] == unit['id']]
                reds = [i for i, r in enumerate(records) if r['phase'] == 'red' and r['assertion_failure']]
                greens = [i for i, r in enumerate(records) if r['phase'] == 'green' and r['exit_code'] == 0]
                evidence = []
                if reds and greens and any(g > r and records[g]['name'] == records[r]['name'] for r in reds for g in greens):
                    evidence = ['red:' + records[reds[0]]['id'], 'green:' + records[greens[-1]]['id']]
                # Visual proof must come from an independent preview harness.
                if unit['validation'] == 'visual':
                    browser = require_browser(self.browser_factory)
                    visual = browser.verify_task(snapshot['commit'], {**unit, 'brain': brain})
                    evidence = visual.evidence
                    visual_checks['visual:' + unit['id']] = visual.passed
                task_evidence[unit['id']] = evidence
            # Check commands themselves must not mutate the reviewed revision.
            if self.workspace.snapshot(path, base) != snapshot:
                raise GateError('Verification changed the workspace')
            return f.set_verification(task_id, Verification(commit=snapshot['commit'],
                checks={**{k: v['exit_code'] == 0 for k, v in checks.items()}, **visual_checks},
                evidence=[r['id'] for r in checks.values()], task_evidence=task_evidence))

        if state == 'REVIEW_PENDING':
            for role in ROLES:
                if role in data['reviews']:
                    continue
                context = f.context(task_id, role, brain)
                context['diff'] = self.workspace.git('diff', '--no-ext-diff', '--no-textconv', base, snapshot['commit'], cwd=path)
                if len(context['diff']) > 120000:
                    raise GateError('Diff too large; split the task for reliable review')
                context['verification'] = data['verification']
                if role == 'ui':
                    browser = require_browser(self.browser_factory)
                    result = browser.review(self._agent(role), context, path)
                else:
                    tools = RepositoryTools(path, writable=False, commands={})
                    result = self._agent(role).run(context, Review, tools)
                if result.role != role:
                    raise GateError('Reviewer returned another role')
                if self.workspace.snapshot(path, base) != snapshot:
                    raise GateError('Reviewer modified workspace')
                f.review(task_id, result)
            return f.approve(task_id)

        if state == 'RISK_PENDING':
            context = f.context(task_id, 'risk', brain)
            context['diff'] = self.workspace.git('diff', '--no-ext-diff', '--no-textconv', base, snapshot['commit'], cwd=path)
            if len(context['diff']) > 120000:
                raise GateError('Diff exceeds risk context budget')
            result = self._agent('risk').run(context, Risk, RepositoryTools(path, writable=False, commands={}))
            return f.classify(task_id, result)
        raise GateError('Unknown state')
