"""Privileged controller adapter. Agents and Docker workers never receive gh auth."""
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from pydantic import Field

from .contracts import Contract
from .engine import Factory, GateError, ROLES
from .workspace import Workspace


class RequiredCheck(Contract):
    name: str = Field(min_length=1, max_length=200)
    app_id: int = Field(gt=0)


class GitHubConfig(Contract):
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
    base: str = Field(default='main', pattern=r'^[A-Za-z0-9][A-Za-z0-9_./-]*$')
    required_checks: list[RequiredCheck] = Field(default_factory=list)


def validate_pr(pr, config, branch, commit):
    expected_url = 'https://github.com/' + config.repository + '/pull/' + str(pr.get('number'))
    if (pr.get('head', {}).get('sha') != commit or pr.get('head', {}).get('ref') != branch
            or pr.get('head', {}).get('repo', {}).get('full_name', '').lower() != config.repository.lower()
            or pr.get('base', {}).get('repo', {}).get('full_name', '').lower() != config.repository.lower()
            or pr.get('base', {}).get('ref') != config.base or pr.get('html_url', '').lower() != expected_url.lower()):
        raise GateError('PR repository, branch or commit does not match reviewed task')


def evaluate_checks(checks, statuses, required, commit):
    if not required:
        return False
    for requirement in required:
        matches = [c for c in checks if c.get('name') == requirement['name']
                   and c.get('app', {}).get('id') == requirement['app_id'] and c.get('head_sha') == commit]
        if not matches or any(c.get('status') != 'completed' or c.get('conclusion') != 'success' for c in matches):
            return False
    # Conservatively block additional failing/pending checks, including legacy statuses.
    return (all(c.get('head_sha') == commit and c.get('status') == 'completed'
                and c.get('conclusion') in ('success', 'neutral', 'skipped') for c in checks)
            and all(s.get('state') == 'success' for s in statuses))


class GitHubClient:
    def __init__(self, config):
        self.config = config
        self.prefix = 'repos/' + config.repository

    def api(self, path, payload=None):
        command = ['gh', 'api', '--hostname', 'github.com', self.prefix + '/' + path,
                   '-H', 'Accept: application/vnd.github+json']
        if payload is not None:
            command += ['--method', 'POST', '--input', '-']
        try:
            result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateError('GitHub unavailable or outcome unknown; inspect before retrying') from exc
        if result.returncode:
            raise GateError('GitHub request failed; verify gh authentication and repository permissions')
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise GateError('Invalid GitHub response') from exc

    def pages(self, path, key=None):
        values = []
        for page in range(1, 101):
            response = self.api(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            batch = response[key] if key else response
            if not isinstance(batch, list):
                raise GateError('Unexpected GitHub pagination response')
            values.extend(batch)
            if len(batch) < 100:
                return values
        raise GateError('GitHub result exceeded pagination limit')

    def ensure_draft(self, branch, commit, title, body):
        if not branch.startswith('codex/') or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise GateError('PR requires a codex branch and immutable commit')
        query = urlencode({'state': 'all', 'head': self.config.repository.split('/')[0] + ':' + branch,
                           'base': self.config.base})
        matches = self.pages('pulls?' + query)
        if len(matches) > 1:
            raise GateError('Multiple PRs match branch; operator inspection required')
        pr = matches[0] if matches else self.api('pulls', {
            'head': branch, 'base': self.config.base, 'title': title[:200], 'body': body, 'draft': True})
        validate_pr(pr, self.config, branch, commit)
        if pr['state'] != 'open':
            raise GateError('Existing PR is closed; refusing to create a duplicate')
        return pr

    def status(self, number, branch, commit):
        if type(number) is not int or number <= 0:
            raise GateError('Invalid PR number')
        pr = self.api('pulls/' + str(number))
        validate_pr(pr, self.config, branch, commit)
        checks = self.pages('commits/' + commit + '/check-runs?filter=latest', 'check_runs')
        statuses = self.pages('commits/' + commit + '/statuses')
        latest = {}
        for status in statuses:
            latest.setdefault(status.get('context'), status)
        # Detect a head change while checks were being fetched.
        validate_pr(self.api('pulls/' + str(number)), self.config, branch, commit)
        return {'url': pr['html_url'], 'commit': commit, 'state': pr['state'], 'draft': pr['draft'],
                'merged': bool(pr.get('merged')), 'mergeable': pr.get('mergeable'),
                'checks_passed': evaluate_checks(checks, list(latest.values()),
                    [r.model_dump() for r in self.config.required_checks], commit),
                'required_checks_configured': bool(self.config.required_checks),
                'checks': [{'name': c.get('name'), 'app_id': c.get('app', {}).get('id'),
                            'status': c.get('status'), 'conclusion': c.get('conclusion')} for c in checks],
                'human_merge_required': True}


class GitHubPublisher:
    def __init__(self, config, client=None):
        if config.github is None:
            raise GateError('Configure github.repository first')
        self.config = config
        self.client = client or GitHubClient(config.github)
        self.factory = Factory(config.database)
        self.workspace = Workspace(config.repo, config.worktrees)

    def _task(self, task_id):
        task = self.factory.get(task_id)
        if Path(task['repo']).resolve() != self.workspace.repo:
            raise GateError('Task repository differs from publisher configuration')
        return task

    def publish(self, task_id):
        from .worker import worker_lock
        with worker_lock(self.config.database):
            task = self._task(task_id)
            if task['state'] not in ('HUMAN_REQUIRED', 'RELEASE_READY'):
                raise GateError('Task must finish verification, four reviews and risk assessment before publication')
            data = task['data']
            commit = data['build']['commit']
            for role in ROLES:
                review = data['reviews'].get(role, {})
                if review.get('verdict') != 'PASS' or review.get('commit') != commit:
                    raise GateError('All four current reviews must pass')
            if data['verification']['commit'] != commit or not all(data['verification']['checks'].values()) or data['risk']['commit'] != commit:
                raise GateError('Stale or failed verification/risk')
            allowed = {'https://github.com/' + self.config.github.repository,
                       'https://github.com/' + self.config.github.repository + '.git',
                       'git@github.com:' + self.config.github.repository + '.git'}
            for args in (('remote', 'get-url', 'origin'), ('remote', 'get-url', '--push', 'origin')):
                if self.workspace.git(*args) not in allowed:
                    raise GateError('Origin differs from configured GitHub repository')
            tree = self.workspace.prepare(task_id, data['build']['base_commit'])
            snapshot = self.workspace.snapshot(tree['path'], data['build']['base_commit'])
            if snapshot['commit'] != commit or snapshot['changed_files'] != data['build']['changed_files']:
                raise GateError('Workspace differs from reviewed build')
            branch = 'codex/factory-' + task_id + '-' + commit[:12]
            ref = 'refs/heads/' + branch
            existing = self.workspace.git('ls-remote', '--heads', 'origin', ref)
            if existing and existing.split()[0] != commit:
                raise GateError('Remote task branch has changed; refusing overwrite')
            if not existing:
                self.workspace.git('-c', 'core.hooksPath=' + str(Path(self.config.artifacts).resolve() / 'no-hooks'),
                                   'push', 'origin', commit + ':' + ref)
            # Do not upload prompts, Brain, logs or screenshots to a public repository.
            body = ('Factory task `' + task_id + '`\n\nReviewed commit: `' + commit + '`\n\n'
                    'Independent verification and all four local reviews passed. '
                    'Remote CI and human approval are still required. No automatic merge or deployment.')
            pr = self.client.ensure_draft(branch, commit, 'Factory task ' + task_id, body)
            record = {'number': pr['number'], 'url': pr['html_url'], 'branch': branch, 'commit': commit}
            if data.get('github_pr') == record:
                return record
            def update(current):
                self.factory._same_commit(current, commit)
                current['data']['github_pr'] = record
            self.factory._change(task_id, ['HUMAN_REQUIRED', 'RELEASE_READY'], 'pr_published', record, update)
            return record

    def status(self, task_id):
        task = self._task(task_id)
        record = task['data'].get('github_pr')
        if not record:
            raise GateError('Task has no published PR')
        if task['data']['build']['commit'] != record['commit']:
            raise GateError('Published PR is stale for current task')
        return self.client.status(record['number'], record['branch'], record['commit'])
