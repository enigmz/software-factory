import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from factory.browser import BrowserConfig, BrowserSession, BrowserHarness
from factory.contracts import Review
from factory.engine import GateError

SHA = 'a' * 40


@pytest.fixture
def preview():
    state = {'commit': SHA, 'external': False}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/.factory/revision':
                body = json.dumps({'commit': state['commit']}).encode()
                content_type = 'application/json'
            else:
                body = b'''<html><body><label>Name <input id="name"></label>
                <button style="position:absolute;left:10px;top:80px;width:140px;height:50px"
                onclick="document.body.style.background='rgb(0, 128, 0)'">Change color</button>
                <div style="height:2000px"></div></body></html>'''
                if state['external']:
                    body += b'<img src="http://127.0.0.1:1/forbidden">'
                content_type = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', state
    server.shutdown()
    server.server_close()
    thread.join()


def test_config_refuses_remote_or_credentialed_preview():
    for url in ('https://example.com', 'http://user:pass@127.0.0.1:8000', 'file:///tmp/a',
                'http://127.0.0.1:8000/path', 'http://127.0.0.1:8000?x=1'):
        with pytest.raises(ValueError):
            BrowserConfig(base_url=url)


def test_wrong_revision_blocks_before_launch(preview, tmp_path):
    url, state = preview
    state['commit'] = 'b' * 40
    with pytest.raises(GateError, match='revision'):
        with BrowserSession(BrowserConfig(base_url=url), SHA, tmp_path):
            pytest.fail('Must not open mismatched preview')


@pytest.mark.browser
def test_real_browser_actions_capture_and_isolation(preview, tmp_path):
    url, _ = preview
    config = BrowserConfig(base_url=url)
    with BrowserSession(config, SHA, tmp_path) as session:
        output = session.call('screenshot', {})
        assert output.content[1]['image_url'].startswith('data:image/png;base64,')
        session.call('press_key', {'key': 'Tab'})
        session.call('type_text', {'text': 'Factory'})
        assert session.page.locator('#name').input_value() == 'Factory'
        session.call('click', {'x': 70, 'y': 105})
        assert session.page.locator('body').evaluate('(e) => getComputedStyle(e).backgroundColor') == 'rgb(0, 128, 0)'
        session.call('viewport', {'device': 'mobile'})
        session.call('screenshot', {})
        assert session.complete()
        directory = session.directory
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['commit'] == SHA
    assert {s['device'] for s in manifest['screenshots']} == {'desktop', 'mobile'}
    assert (directory / 'trace.zip').is_file()
    with BrowserSession(config, SHA, tmp_path) as other:
        assert other.page.locator('#name').input_value() == ''


@pytest.mark.browser
def test_revision_change_and_external_navigation_cannot_pass(preview, tmp_path):
    url, state = preview
    with BrowserSession(BrowserConfig(base_url=url), SHA, tmp_path) as session:
        with pytest.raises(GateError):
            session.call('navigate', {'path': 'https://example.com'})
        assert not session.complete()
    with BrowserSession(BrowserConfig(base_url=url), SHA, tmp_path) as session:
        state['commit'] = 'b' * 40
        with pytest.raises(GateError, match='revision'):
            session.call('screenshot', {})
        assert not session.complete()


@pytest.mark.browser
def test_external_subresource_is_blocked(preview, tmp_path):
    url, state = preview
    state['external'] = True
    with BrowserSession(BrowserConfig(base_url=url), SHA, tmp_path) as session:
        assert session.violations
        assert not session.complete()


@pytest.mark.browser
def test_agent_cannot_pass_without_visual_observation(preview, tmp_path):
    url, _ = preview
    class Agent:
        def run(self, context, contract, tools):
            return Review(role='ui', commit=SHA, verdict='PASS', summary='Invented success', evidence=['invented'])
    harness = BrowserHarness(BrowserConfig(base_url=url), tmp_path, lambda: Agent())
    review = harness.review(Agent(), {'role': 'ui', 'commit': SHA}, '')
    assert review.verdict == 'BLOCKED'
    assert review.evidence != ['invented']


@pytest.mark.browser
def test_visual_task_records_only_real_evidence(preview, tmp_path):
    url, _ = preview
    class Agent:
        def run(self, context, contract, tools):
            tools.call('screenshot', {})
            tools.call('viewport', {'device': 'mobile'})
            tools.call('screenshot', {})
            return Review(role='ui', commit=SHA, verdict='PASS', summary='Observed both sizes', evidence=['invented'])
    harness = BrowserHarness(BrowserConfig(base_url=url), tmp_path, lambda: Agent())
    result = harness.verify_task(SHA, {'id': 'color', 'acceptance': ['Green background']})
    assert result.passed
    assert all(e.startswith('visual:') for e in result.evidence)
