"""Trusted worker image entrypoint. No API keys or controller state are present."""
import base64
import functools
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, '/opt')


def check(command):
    shutil.copytree('/input', '/workspace', dirs_exist_ok=True)
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(command, cwd='/workspace', stdout=output, stderr=subprocess.STDOUT)
        size = output.tell()
        output.seek(max(0, size - 1000000))
        if size > 1000000:
            print('[Output truncated to last 1 MB]')
        print(output.read().decode('utf-8', errors='replace'), end='')
    return result.returncode


def request():
    payload = sys.stdin.buffer.read(100000)
    connection = http.client.HTTPConnection('127.0.0.1', 9000, timeout=35)
    connection.request('POST', '/', payload, {'Content-Type': 'application/json'})
    response = connection.getresponse()
    body = response.read(32_000_001)
    if len(body) > 32_000_000:
        raise ValueError('Worker response limit exceeded')
    sys.stdout.buffer.write(body)
    connection.close()


def browser(settings):
    from factory.browser import BrowserConfig, BrowserSession
    from factory.tool_output import ToolContent
    class Preview(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/.factory/revision':
                body = json.dumps({'commit': settings['commit']}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Preview, directory='/input'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = BrowserConfig.model_validate({**settings['browser'], 'base_url': f'http://127.0.0.1:{server.server_port}'})
    try:
        with BrowserSession(config, settings['commit'], '/tmp/artifacts') as session:
            # Outer Docker boundary is mandatory. Chromium's inner sandbox remains enabled.
            class RPC(BaseHTTPRequestHandler):
                def do_POST(self):
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        if not 0 < length <= 100000:
                            raise ValueError('Request limit')
                        message = json.loads(self.rfile.read(length))
                        if message['op'] == 'ping':
                            result = {'ready': True}
                        elif message['op'] == 'call':
                            value = session.call(message['name'], message['arguments'])
                            result = {'multimodal': isinstance(value, ToolContent),
                                      'value': value.content if isinstance(value, ToolContent) else value}
                        elif message['op'] == 'complete':
                            result = {'complete': session.complete()}
                        elif message['op'] == 'export':
                            session._manifest()
                            session.context.tracing.stop(path=str(session.directory / 'trace.zip'))
                            files = {}
                            total = 0
                            for path in session.directory.iterdir():
                                if path.suffix not in ('.json', '.png', '.zip') or path.is_symlink():
                                    continue
                                total += path.stat().st_size
                                if total > 20000000:
                                    raise ValueError('Artifact limit')
                                files[path.name] = base64.b64encode(path.read_bytes()).decode()
                            result = {'files': files}
                        else:
                            raise ValueError('Unknown operation')
                        output = {'ok': True, 'result': result}
                    except Exception as exc:
                        output = {'ok': False, 'error': type(exc).__name__}
                    body = json.dumps(output).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args): pass
            with HTTPServer(('127.0.0.1', 9000), RPC) as rpc:
                rpc.serve_forever()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    if sys.argv[1] == 'check':
        sys.exit(check(sys.argv[2:]))
    elif sys.argv[1] == 'browser':
        browser(json.loads(sys.argv[2]))
    elif sys.argv[1] == 'request':
        request()
    else:
        raise ValueError('Unknown runner mode')
