import re
import subprocess
from pathlib import Path

from .engine import GateError


class Workspace:
    def __init__(self, repo, root):
        self.repo = Path(repo).resolve()
        self.root = Path(root).resolve()

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', '-C', str(cwd or self.repo), *args],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise GateError(result.stderr.strip())
        return result.stdout.strip()

    def prepare(self, task_id, base):
        if not re.fullmatch(r'[a-f0-9]{16,32}', task_id) or not re.fullmatch(r'[a-f0-9]{40}', base):
            raise GateError('Worktree requires a task ID and immutable base commit')
        self.git('cat-file', '-e', base + '^{commit}')
        branch = 'codex/factory-' + task_id
        path = self.root / task_id
        if path.exists():
            if self.git('rev-parse', '--show-toplevel', cwd=path).replace('\\', '/') != str(path).replace('\\', '/'):
                raise GateError('Existing path is not the expected worktree')
            if self.git('branch', '--show-current', cwd=path) != branch:
                raise GateError('Existing worktree branch mismatch')
            self.git('merge-base', '--is-ancestor', base, 'HEAD', cwd=path)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.git('worktree', 'add', '-b', branch, str(path), base)
        return {'path': str(path), 'branch': branch, 'base_commit': base}

    def snapshot(self, path, base):
        if self.git('status', '--porcelain', cwd=path):
            raise GateError('Builder must commit all changes before verification')
        commit = self.git('rev-parse', 'HEAD', cwd=path)
        self.git('merge-base', '--is-ancestor', base, commit, cwd=path)
        # Include both old and new paths for renames, so a sensitive deletion cannot hide.
        files = self.git('diff', '--no-renames', '--name-only', base, commit, cwd=path).splitlines()
        if not files:
            raise GateError('Build has no changes')
        if any(p == 'brain' or p.startswith('brain/') for p in files):
            raise GateError('Builder changed Brain; separate architecture approval required')
        return {'commit': commit, 'changed_files': files}
