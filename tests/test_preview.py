import subprocess

import pytest

from factory.preview import GitPreview
from factory.engine import GateError


def test_preview_serves_committed_bytes_not_changed_checkout(tmp_path):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init')
    public = tmp_path / 'public'
    public.mkdir()
    page = public / 'index.html'
    page.write_text('<h1>Committed</h1>')
    (tmp_path / '.env').write_text('PRIVATE=value')
    git('add', 'public')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'preview')
    commit = git('rev-parse', 'HEAD')
    preview = GitPreview(tmp_path, commit, 'public')
    page.write_text('<h1>Uncommitted</h1>')
    assert preview.content('/')[2] == b'<h1>Committed</h1>'
    assert preview.content('/.factory/revision')[2].decode() == '{"commit": "' + commit + '"}'
    for path in ('/../.env', '/%2e%2e/.env', '/.env', '/missing', '/%5c../.env'):
        assert preview.content(path)[0] == 404
    with pytest.raises(GateError):
        GitPreview(tmp_path, 'HEAD', 'public')
