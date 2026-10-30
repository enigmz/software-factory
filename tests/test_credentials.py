import pytest

from factory import credentials
from factory.engine import GateError


def test_project_env_is_loaded_without_exporting_secret(monkeypatch, tmp_path):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    env_file = tmp_path / '.env'
    env_file.write_text('# local\nOPENAI_API_KEY="test-only"\nUNRELATED=value\n')
    monkeypatch.setattr(credentials, 'PROJECT_ENV', env_file)
    assert credentials.openai_api_key() == 'test-only'
    assert 'OPENAI_API_KEY' not in credentials.os.environ


def test_process_environment_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY', 'environment-test')
    monkeypatch.setattr(credentials, 'PROJECT_ENV', tmp_path / 'missing')
    assert credentials.openai_api_key() == 'environment-test'


def test_empty_key_and_duplicate_handling(monkeypatch, tmp_path):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    env_file = tmp_path / '.env'
    monkeypatch.setattr(credentials, 'PROJECT_ENV', env_file)
    env_file.write_text('OPENAI_API_KEY=\n')
    assert credentials.openai_api_key() is None
    env_file.write_text('OPENAI_API_KEY=one\nOPENAI_API_KEY=two\n')
    with pytest.raises(GateError, match='Duplicate'):
        credentials.openai_api_key()
