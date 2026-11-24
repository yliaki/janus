"""What the About pages show (desktop GUI: Settings → About; Big Picture:
Start → Settings → About)."""

import subprocess
from pathlib import Path

import version

AUTHOR = 'ilyaki'
AUTHOR_URL = 'https://github.com/ilyaki'
# Also the website's Donate page (HUB_KOFI_URL). Empty: "coming soon".
KOFI_URL = 'https://ko-fi.com/ilyaki'

_ROOT = Path(__file__).resolve().parent.parent.parent


def version_text() -> str:
    """'1.0.0' for a release, '1.0.0+git20261123.abc1234' for another
    package, '1.0.0+dev (a34bd9b)' for a git checkout."""
    v = version.janus_version()
    if v.endswith('+dev'):
        try:
            commit = subprocess.run(
                ['git', '-C', str(_ROOT), 'rev-parse', '--short', 'HEAD'],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ''
        if commit:
            v += f' ({commit})'
    return v
