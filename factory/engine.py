"""Deterministic gates. Only trusted adapters may submit evidence to this API.

SQLite transactions serialize transitions and their audit events. Agent output
never supplies a state transition or an authorization policy.
"""
import fnmatch
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .contracts import Build, Review, Risk, Spec, Verification

ROLES = ('security', 'ux', 'ui', 'consistency')
MISSIONS = {
    'spec': 'Write a bounded specification with independently verifiable acceptance criteria. Consult Brain.',
    'builder': 'Implement every spec task, red/green/refactor for behavior, preserve Brain, request all four reviews.',
    'security': 'Inspect permissions, data isolation, secrets, inputs and dependencies. Report concrete evidence.',
    'ux': 'Verify completion, recovery, error and empty states against acceptance criteria.',
    'ui': 'Use a controlled browser on the exact preview commit. Verify desktop/mobile, contrast and keyboard.',
    'consistency': 'Compare architecture, code, comments and tests against the versioned Brain.',
    'risk': 'Assess actual diff, blast radius, reversibility and uncertainty. Never authorize a merge.',
}


class GateError(ValueError):
    pass


class Factory:
    def __init__(self, path, *, auto_merge=False, auto_paths=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.auto_merge = auto_merge
        self.auto_paths = tuple(auto_paths)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, event_key TEXT NOT NULL,
                    repo TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL,
                    data TEXT NOT NULL, UNIQUE(source,event_key,repo));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        if row is None:
            raise GateError('Unknown task')
        return {**dict(row), 'data': json.loads(row['data'])}

    @staticmethod
    def _event(db, task, kind, payload):
        db.execute('INSERT INTO events(task_id,at,kind,state,payload) VALUES (?,?,?,?,?)',
                   (task['id'], datetime.now(timezone.utc).isoformat(), kind, task['state'], json.dumps(payload)))

    def submit(self, source, event_key, repo, prompt):
        if source not in ('prompt', 'git', 'sentry') or not all(isinstance(v, str) and v.strip() for v in (event_key, repo, prompt)):
            raise GateError('Source, repository, event key and prompt are required')
        if len(prompt) > 100000:
            raise GateError('Input too large')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE source=? AND event_key=? AND repo=?', (source, event_key, repo)).fetchone()
            if row:
                task = self._decode(row)
                if task['prompt'] != prompt:
                    raise GateError('Duplicate event key with different payload')
                return task
            task = dict(id=uuid.uuid4().hex, source=source, event_key=event_key, repo=repo,
                        prompt=prompt, state='SPEC_PENDING', data={'attempts': 0, 'reviews': {}})
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',
                       tuple(task[k] for k in ('id', 'source', 'event_key', 'repo', 'prompt', 'state')) + (json.dumps(task['data']),))
            self._event(db, task, 'submitted', {'source': source})
            return task

    def get(self, task_id):
        with self._db() as db:
            return self._decode(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def events(self, task_id):
        with self._db() as db:
            return [{**dict(r), 'payload': json.loads(r['payload'])} for r in
                    db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq', (task_id,))]

    def _change(self, task_id, states, kind, payload, update):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._decode(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
            if task['state'] not in states:
                raise GateError(f"Cannot {kind} from {task['state']}")
            update(task)
            db.execute('UPDATE tasks SET state=?,data=? WHERE id=?', (task['state'], json.dumps(task['data']), task_id))
            self._event(db, task, kind, payload)
            return task

    @staticmethod
    def _same_commit(task, commit):
        if task['data']['build']['commit'] != commit:
            raise GateError('Evidence commit does not match current build commit')

    @staticmethod
    def _rework(task, feedback):
        data = task['data']
        data['attempts'] += 1
        data['feedback'] = feedback
        data['reviews'] = {}
        for key in ('verification', 'risk', 'human_approval'):
            data.pop(key, None)
        task['state'] = 'ESCALATED' if data['attempts'] >= 3 else 'BUILD_PENDING'

    def set_spec(self, task_id, spec: Spec):
        spec = Spec.model_validate(spec.model_dump())
        def update(task):
            task['data']['spec'] = spec.model_dump()
            task['state'] = 'BUILD_PENDING'
        return self._change(task_id, ['SPEC_PENDING'], 'specified', spec.model_dump(), update)

    def set_build(self, task_id, build: Build):
        def update(task):
            if build.brain_revision != task['data']['spec']['brain_revision']:
                raise GateError('Brain revision changed; specification must be revisited')
            task['data']['build'] = build.model_dump()
            task['data']['reviews'] = {}
            task['state'] = 'VERIFY_PENDING'
        return self._change(task_id, ['BUILD_PENDING'], 'built', build.model_dump(), update)

    def set_verification(self, task_id, verification: Verification):
        def update(task):
            self._same_commit(task, verification.commit)
            if not all(verification.checks.get(k) is True for k in ('tests', 'lint', 'acceptance')) or not all(verification.checks.values()):
                self._rework(task, {'verification': verification.model_dump()})
                return
            for unit in task['data']['spec']['tasks']:
                evidence = verification.task_evidence.get(unit['id'], [])
                required = ('red:', 'green:') if unit['validation'] == 'tdd' else ('visual:',)
                if not all(any(e.startswith(prefix) and len(e) > len(prefix) for e in evidence) for prefix in required):
                    raise GateError(f"Missing {'TDD' if unit['validation'] == 'tdd' else 'visual'} evidence for {unit['id']}")
            task['data']['verification'] = verification.model_dump()
            task['state'] = 'REVIEW_PENDING'
        return self._change(task_id, ['VERIFY_PENDING'], 'verified', verification.model_dump(), update)

    def review(self, task_id, review: Review):
        def update(task):
            self._same_commit(task, review.commit)
            existing = task['data']['reviews'].get(review.role)
            if existing and existing != review.model_dump():
                raise GateError('Review cannot be overwritten for the same build')
            task['data']['reviews'][review.role] = review.model_dump()
        return self._change(task_id, ['REVIEW_PENDING'], 'reviewed', review.model_dump(), update)

    def approve(self, task_id):
        def update(task):
            reviews = task['data']['reviews']
            if set(reviews) != set(ROLES):
                raise GateError('All four independent reviews are required')
            if any(r['verdict'] != 'PASS' for r in reviews.values()):
                self._rework(task, dict(reviews))
            else:
                task['state'] = 'RISK_PENDING'
        return self._change(task_id, ['REVIEW_PENDING'], 'approved', {}, update)

    def policy_allows(self, paths):
        # Initial automatic scope is deliberately limited to documentation and CSS.
        sensitive = re.compile(r'auth|permission|payment|billing|secret|token|migration|infra|deploy|security', re.I)
        return bool(paths) and all(
            not p.startswith(('/', '.', '\\')) and '\\' not in p and ':' not in p
            and '..' not in p.split('/') and not sensitive.search(p)
            and Path(p).suffix in ('.md', '.css')
            and any(fnmatch.fnmatchcase(p, pattern) for pattern in self.auto_paths)
            for p in paths)

    def classify(self, task_id, risk: Risk):
        def update(task):
            self._same_commit(task, risk.commit)
            task['data']['risk'] = risk.model_dump()
            automatic = (self.auto_merge and risk.level == 'low' and risk.reversible and not risk.uncertain
                         and self.policy_allows(task['data']['build']['changed_files']))
            task['state'] = 'RELEASE_READY' if automatic else 'HUMAN_REQUIRED'
        return self._change(task_id, ['RISK_PENDING'], 'risk_classified', risk.model_dump(), update)

    def human_approve(self, task_id, commit, actor):
        if not actor.strip():
            raise GateError('An identified human is required')
        def update(task):
            self._same_commit(task, commit)
            task['data']['human_approval'] = {'actor': actor, 'commit': commit}
            task['state'] = 'RELEASE_READY'
        return self._change(task_id, ['HUMAN_REQUIRED'], 'human_approved', {'actor': actor, 'commit': commit}, update)

    def context(self, task_id, role, brain):
        if role not in MISSIONS:
            raise GateError('Unknown role')
        task = self.get(task_id)
        data = task['data']
        if 'spec' in data and brain['revision'] != data['spec']['brain_revision']:
            raise GateError('Brain revision mismatch')
        context = {'role': role, 'mission': MISSIONS[role], 'task_id': task_id, 'brain': brain,
                   'untrusted_input': task['prompt'], 'spec': data.get('spec'),
                   'requirements': {'computer_use': role == 'ui', 'input_is_data': True,
                                    'review_only': role in ROLES}}
        if 'build' in data:
            context.update(commit=data['build']['commit'], base_commit=data['build']['base_commit'],
                           changed_files=data['build']['changed_files'])
        if role == 'builder':
            context['feedback'] = data.get('feedback', {})
        return context
