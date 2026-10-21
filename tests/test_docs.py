"""Keep public docs honest: links resolve, API routes exist, settings and formats are named."""

import re
from dataclasses import fields
from pathlib import Path

from audiolab import __version__
from audiolab.api import create_app
from audiolab.media.export import SEPARATE_FORMATS
from audiolab.settings import Limits, UserSettings

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = [
    *(ROOT / name for name in ('README.md', 'CONTRIBUTING.md', 'CHANGELOG.md', 'SECURITY.md')),
    *sorted((ROOT / 'docs').glob('*.md')),
    ROOT / 'audiolab' / 'web' / 'fonts' / 'README.md',
]
API_DOC = ROOT / 'docs' / 'API.md'
LINK_RE = re.compile(r'!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')


def _normalise(path: str) -> str:
    """Path parameter names differ (docs say {id}, FastAPI says {job_id}); their shape must match."""
    return re.sub(r'\{[^}]+\}', '{}', path)


def test_relative_markdown_links_resolve():
    missing = []
    for path in DOC_FILES:
        text = path.read_text(encoding='utf-8')
        for target in LINK_RE.findall(text):
            target = target.strip('<>')
            if target.startswith(('#', 'http://', 'https://', 'mailto:', 'tel:')):
                continue
            target = target.split('#', 1)[0]
            if target and not (path.parent / target).resolve().exists():
                missing.append(f'{path.relative_to(ROOT)} -> {target}')
    assert not missing, 'Broken documentation links:\n' + '\n'.join(missing)


def test_api_documents_every_route():
    text = API_DOC.read_text(encoding='utf-8')
    documented = {
        (method, _normalise(path)) for method, path in re.findall(r'`(GET|POST|PUT|DELETE) (/api/[^`]+)`', text)
    }
    app = create_app(local=True)
    actual = {
        (method.upper(), _normalise(path))
        for path, operations in app.openapi()['paths'].items()
        for method in operations
    }
    assert documented == actual, (
        f'Undocumented routes: {sorted(actual - documented)}\n'
        f'Routes that no longer exist: {sorted(documented - actual)}'
    )


def test_api_documents_current_version_and_settings():
    text = API_DOC.read_text(encoding='utf-8')
    assert f'"version": "{__version__}"' in text
    for field in fields(UserSettings):
        assert f'"{field.name}"' in text, f'Settings field {field.name} is missing from docs/API.md'


def test_api_documents_all_split_formats():
    text = API_DOC.read_text(encoding='utf-8')
    for key in SEPARATE_FORMATS:
        assert f'"{key}"' in text, f'Split format {key} is missing from docs/API.md'


def test_usage_environment_variables_are_current():
    text = (ROOT / 'docs' / 'USAGE.md').read_text(encoding='utf-8')
    documented = set(re.findall(r'AUDIOLAB_([A-Z0-9_]+)', text))
    known = {'OUTPUT_DIR', 'DATA_DIR', 'TOKEN'} | {field.name.upper() for field in fields(Limits)}
    assert documented == known, f'docs/USAGE.md env vars differ: {sorted(documented ^ known)}'


def test_usage_cli_commands_exist():
    text = (ROOT / 'docs' / 'USAGE.md').read_text(encoding='utf-8')
    documented = set(re.findall(r'^audiolab (\w+)', text, re.MULTILINE))
    cli = (ROOT / 'audiolab' / 'cli.py').read_text(encoding='utf-8')
    commands = set(re.findall(r"add_parser\('([^']+)'", cli))
    assert documented <= commands, f'Unknown docs/USAGE.md commands: {sorted(documented - commands)}'


def test_windows_launcher_uses_crlf():
    data = (ROOT / 'start.bat').read_bytes()
    assert b'\r\n' in data and b'\n' not in data.replace(b'\r\n', b'')
