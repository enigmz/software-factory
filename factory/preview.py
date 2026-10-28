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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .engine import GateError


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
