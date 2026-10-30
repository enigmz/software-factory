"""Serve static, tracked files directly from an immutable Git revision on loopback.

No checkout is served, so later working-tree edits cannot change the preview.
For dynamic applications, supply an isolated preview service with the same
/.factory/revision contract, implemented by trusted deployment infrastructure.
"""
import argparse
import json
import mimetypes
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .engine import GateError
from .contracts import Contract
import httpx
from pydantic import Field, field_validator


class PreviewConfig(Contract):
    directory: str
    startup_timeout: float = Field(default=10.0, ge=0.1, le=60)

    @field_validator('directory')
    @classmethod
    def public_directory(cls, value):
        if not value or value.startswith(('/', '.')) or '\\' in value or ':' in value or any(
                part in ('', '.', '..') or part.startswith('.') for part in value.split('/')):
            raise ValueError('Preview directory must be a public repository-relative directory')
        return value


class ManagedPreview:
    """Own one loopback server per check; daemon threads cannot outlive the worker.

    There is no application subprocess to orphan or external PID to terminate.
    A fresh port avoids collisions with manual previews and other tasks.
    """
    def __init__(self, repo, commit, config, artifacts):
        self.repo, self.commit, self.config = repo, commit, config
        self.manifest = Path(artifacts).resolve() / uuid.uuid4().hex / 'preview.json'
        self.server = self.thread = None
        self.url = None
        self.record = {'commit': commit, 'directory': config.directory, 'state': 'starting',
                       'outcome': 'pending', 'started_at': datetime.now(timezone.utc).isoformat()}

    def _save(self):
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.record, indent=2), encoding='utf-8')
        temporary.replace(self.manifest)

    def __enter__(self):
        self._save()
        try:
            preview = GitPreview(self.repo, self.commit, self.config.directory)
            self.server = preview.server(0)
            self.url = f'http://127.0.0.1:{self.server.server_port}'
            self.thread = threading.Thread(target=self.server.serve_forever,
                kwargs={'poll_interval': 0.05}, daemon=True, name='factory-preview')
            self.thread.start()
            self._ready()
            self.record.update(state='ready', url=self.url)
            self._save()
            return self
        except BaseException:
            self._close('error')
            raise

    def _ready(self):
        deadline = time.monotonic() + self.config.startup_timeout
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            while time.monotonic() < deadline:
                remaining = max(0.01, deadline - time.monotonic())
                try:
                    revision = client.get(self.url + '/.factory/revision', timeout=min(1, remaining))
                    if revision.status_code != 200 or revision.json() != {'commit': self.commit}:
                        raise GateError('Preview revision mismatch')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    page = client.get(self.url, timeout=min(1, remaining))
                    if page.status_code != 200:
                        raise GateError('Preview entry page is unavailable; check directory/index.html')
                    if time.monotonic() <= deadline:
                        return
                except httpx.TransportError:
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                except ValueError as exc:
                    raise GateError('Preview revision is invalid') from exc
        raise GateError('Preview readiness timed out')

    def _close(self, outcome):
        try:
            if self.server:
                if self.thread and self.thread.is_alive():
                    self.server.shutdown()
                self.server.server_close()
                if self.thread:
                    self.thread.join(timeout=2)
                    if self.thread.is_alive():
                        raise GateError('Preview thread failed to stop')
        finally:
            self.record.update(state='stopped', outcome=outcome, url=self.url,
                               stopped_at=datetime.now(timezone.utc).isoformat())
            self._save()

    def __exit__(self, exc_type, exc, traceback):
        self._close('error' if exc_type else 'success')


class GitPreview:
    def __init__(self, repo, commit, directory):
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise GateError('Preview requires a full commit SHA')
        if not directory or directory.startswith(('/', '.')) or '\\' in directory or ':' in directory or '..' in directory.split('/'):
            raise GateError('Preview directory must be a repository-relative public directory')
        self.repo, self.commit, self.directory = repo, commit, directory.rstrip('/')
        self.git('cat-file', '-e', commit + '^{commit}')
        if self.git('cat-file', '-t', commit + ':' + self.directory).strip() != b'tree':
            raise GateError('Preview directory is not a committed Git tree')

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, timeout=10)
        if result.returncode:
            raise GateError('Git preview object is unavailable')
        return result.stdout

    def content(self, raw_path):
        path = unquote(urlsplit(raw_path).path)
        if path == '/.factory/revision':
            return 200, 'application/json', json.dumps({'commit': self.commit}).encode()
        parts = PurePosixPath(path.lstrip('/')).parts
        if not path.startswith('/') or '\\' in path or ':' in path or any(p.startswith('.') for p in parts):
            return 404, 'text/plain', b'Not found'
        relative = '/'.join(parts)
        if path.endswith('/') or not relative:
            relative += ('/' if relative else '') + 'index.html'
        target = self.directory + '/' + relative
        try:
            entry = self.git('ls-tree', self.commit, '--', target).decode().split()
            if not entry or entry[0] not in ('100644', '100755') or entry[1] != 'blob':
                return 404, 'text/plain', b'Not found'
            object_id = entry[2]
            if int(self.git('cat-file', '-s', object_id)) > 10_000_000:
                return 413, 'text/plain', b'Asset exceeds preview limit'
            body = self.git('cat-file', 'blob', object_id)
        except GateError:
            return 404, 'text/plain', b'Not found'
        return 200, mimetypes.guess_type(relative)[0] or 'application/octet-stream', body

    def server(self, port):
        preview = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, content_type, body = preview.content(self.path)
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Immutable Git static preview (loopback only)')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    preview = GitPreview(args.repo, args.commit, args.directory)
    with preview.server(args.port) as server:
        print(f'Preview http://127.0.0.1:{server.server_port} · commit {args.commit}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
