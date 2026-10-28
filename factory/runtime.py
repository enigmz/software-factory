import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

from .engine import GateError


def function(name, description, fields):
    return {'type': 'function', 'name': name, 'description': description, 'strict': True,
            'parameters': {'type': 'object', 'properties': {k: {'type': 'string'} for k in fields},
                           'required': list(fields), 'additionalProperties': False}}


class RepositoryTools:
    """Capability-limited file tools, not an OS sandbox. Execute only in a worker VM."""
    def __init__(self, root, *, writable, commands, artifact_dir=None, journal=None):
        self.root = Path(root).resolve()
        self.writable = writable
        self.commands = commands
        self.artifact_dir = Path(artifact_dir).resolve() if artifact_dir else None
        self.journal = journal if journal is not None else []

    def definitions(self):
        definitions = [function('list_files', 'List source file paths', []),
                       function('read_file', 'Read a UTF-8 source file', ['path'])]
        if self.writable:
            definitions.extend([function('write_file', 'Write a UTF-8 source file', ['path', 'content']),
                function('run_check', 'Run an operator-configured check. phase: red, green or check.', ['name', 'task_id', 'phase'])])
        return definitions

    def _path(self, value, write=False):
        relative = Path(value)
        if not value or relative.is_absolute() or '\\' in value or ':' in value or '..' in relative.parts:
            raise GateError('Path escapes repository')
        denied = {'.git', '.github', '.codex', '.agents', '.venv', 'node_modules', 'data'}
        if any(p in denied or p.startswith('.env') for p in relative.parts):
            raise GateError('Protected path')
        if write and relative.parts[0] in ('brain', 'factory'):
            raise GateError('Protected policy or control plane')
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise GateError('Symlink escapes repository')
        return path

    def call(self, name, arguments):
        if name == 'list_files':
            found = []
            for parent, dirs, files in os.walk(self.root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in ('.git', '.venv', 'node_modules', 'data') and not d.startswith('.')]
                for file in files:
                    relative = (Path(parent) / file).relative_to(self.root).as_posix()
                    try:
                        self._path(relative)
                    except GateError:
                        continue
                    found.append(relative)
                    if len(found) > 3000:
                        raise GateError('Repository inventory too large; narrow project scope')
            return sorted(found)
        if name == 'read_file':
            path = self._path(arguments['path'])
            if path.stat().st_size > 100000:
                raise GateError('File exceeds read limit')
            return path.read_text(encoding='utf-8')
        if not self.writable:
            raise GateError('Reviewer tools are read-only')
        if name == 'write_file':
            path = self._path(arguments['path'], write=True)
            content = arguments['content']
            if len(content) > 100000:
                raise GateError('File exceeds write limit')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            return {'written': arguments['path']}
        if name == 'run_check':
            return self.run_check(**arguments)
        raise GateError('Unknown tool')

    def run_check(self, name, task_id='', phase='check'):
        if name not in self.commands or phase not in ('red', 'green', 'check'):
            raise GateError('Unknown configured check or phase')
        command = self.commands[name]
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise GateError('Checks must be argument arrays, never shell strings')
        # No API keys or deployment credentials are inherited by test processes.
        allowed = ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'VIRTUAL_ENV')
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        try:
            result = subprocess.run(command, cwd=self.root, env=env, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=180, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise GateError('Check timeout; no passing evidence recorded') from exc
        output = result.stdout + '\n' + result.stderr
        record = {'id': uuid.uuid4().hex, 'name': name, 'task_id': task_id, 'phase': phase,
                  'exit_code': result.returncode, 'output_sha256': hashlib.sha256(output.encode()).hexdigest(),
                  'assertion_failure': result.returncode == 1 and 'AssertionError' in output and 'ERROR collecting' not in output}
        if self.artifact_dir:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            (self.artifact_dir / (record['id'] + '.log')).write_text(output, encoding='utf-8')
        self.journal.append(record)
        return {**record, 'output': output[-24000:]}
