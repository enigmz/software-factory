"""Docker execution boundary. No project process runs on the controller in this mode."""
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract
from .engine import GateError

EXCLUDED = {'.git', '.github', '.codex', '.agents', '.ssh', '.aws', '.docker', '.kube', '.npmrc', '.netrc', '.venv', 'venv',
            'node_modules', 'data', '__pycache__', '.pytest_cache', '.ruff_cache'}


class IsolationConfig(Contract):
    mode: Literal['docker', 'local'] = 'docker'
    image: str = Field(default='software-factory-worker:local', pattern=r'^[a-zA-Z0-9][a-zA-Z0-9./:@_-]{0,240}$')
    memory_mb: int = Field(default=1536, ge=512, le=4096)
    cpus: int = Field(default=2, ge=1, le=4)
    timeout_seconds: int = Field(default=180, ge=5, le=900)


def excluded(parts):
    return any(p.lower() in EXCLUDED or p.lower().startswith('.env') for p in parts) or Path(parts[-1]).suffix.lower() in ('.pem', '.key', '.sqlite3')


def copy_source(source, target, *, max_bytes=100_000_000):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.is_relative_to(source):
        raise GateError('Snapshot must be outside source repository')
    target.mkdir(parents=True, exist_ok=True)
    count = size = 0
    for parent, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if not excluded((name,))]
        for name in dirs + files:
            path = Path(parent) / name
            relative = path.relative_to(source)
            if excluded(relative.parts):
                continue
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise GateError('Links are not allowed in isolated snapshots')
            if name in dirs:
                continue
            if not path.is_file() or path.stat().st_nlink != 1:
                raise GateError('Snapshot requires regular unlinked files')
            count += 1
            size += path.stat().st_size
            if count > 5000 or size > max_bytes:
                raise GateError('Snapshot size limit exceeded')
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


def docker_flags(config, name, snapshot):
    source = str(Path(snapshot).resolve())
    if ',' in source:
        raise GateError('Snapshot mount path cannot contain commas')
    return ['--name', name, '--label', 'software-factory.worker=true', '--pull', 'never',
        '--network', 'none', '--read-only', '--user', '10001:10001', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges=true', '--init', '--pids-limit', '256',
        '--memory', f'{config.memory_mb}m', '--memory-swap', f'{config.memory_mb}m',
        '--cpus', str(config.cpus), '--shm-size', '256m',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=1777',
        '--tmpfs', '/workspace:rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700',
        '--mount', f'type=bind,source={source},target=/input,readonly',
        '--workdir', '/workspace', '--env', 'HOME=/tmp', '--env', 'PYTHONDONTWRITEBYTECODE=1',
        '--env', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1', '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1']


class DockerExecutor:
    def __init__(self, config, artifacts):
        self.config, self.artifacts = config, Path(artifacts)

    def docker(self, args, *, timeout=30, check=True, input=None):
        try:
            result = subprocess.run(['docker', *args], input=input, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=timeout, shell=False)
        except (FileNotFoundError, OSError) as exc:
            raise GateError('Docker is unavailable; host fallback is disabled') from exc
        if check and result.returncode:
            raise GateError('Docker operation failed (' + args[0] + '); host fallback is disabled')
        return result

    def preflight(self):
        if self.docker(['version', '--format', '{{.Server.Os}}']).stdout.strip() != 'linux':
            raise GateError('Docker Linux engine is required')
        image = self.docker(['image', 'inspect', '--format', '{{.Id}}', self.config.image]).stdout.strip()
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise GateError('Docker image is unavailable; build the worker image first')
        return image

    def remove(self, name):
        self.docker(['rm', '--force', name], check=True)

    def run(self, root, command):
        if not command or ':' in command[0] or command[0].startswith(('/', '\\')):
            raise GateError('Isolated checks require an executable in the image, e.g. python, not a host path')
        image = self.preflight()
        self.artifacts.mkdir(parents=True, exist_ok=True)
        name = 'factory-' + uuid.uuid4().hex
        staging = tempfile.mkdtemp(prefix='snapshot-', dir=self.artifacts)
        created = False
        try:
            snapshot = Path(staging) / 'source'
            copy_source(root, snapshot)
            try:
                self.docker(['create', *docker_flags(self.config, name, snapshot), image, 'check', *command])
                created = True
                result = self.docker(['start', '--attach', name], timeout=self.config.timeout_seconds, check=False)
                if result.returncode in (125, 126, 127):
                    raise GateError('Isolated runner could not execute the configured check')
                return result
            except subprocess.TimeoutExpired as exc:
                raise GateError('Isolated check timeout; container terminated') from exc
            finally:
                if created:
                    self.remove(name)
                    created = False
        finally:
            # Preserve the mount if removal failed; never mutate a live worker's input.
            if not created:
                shutil.rmtree(staging)
