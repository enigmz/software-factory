"""Read the controller's local API key, never the target repository's .env."""
import os
from pathlib import Path

from .engine import GateError

PROJECT_ENV = Path(__file__).resolve().parents[1] / '.env'


def openai_api_key():
    existing = os.environ.get('OPENAI_API_KEY', '').strip()
    if existing:
        return existing
    if not PROJECT_ENV.is_file():
        return None
    result = None
    for line in PROJECT_ENV.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if not separator or name.strip() != 'OPENAI_API_KEY':
            continue
        if result is not None:
            raise GateError('Duplicate OPENAI_API_KEY in project .env')
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise GateError('Invalid quoting for OPENAI_API_KEY in project .env')
            value = value[1:-1].strip()
        result = value
    return result or None
