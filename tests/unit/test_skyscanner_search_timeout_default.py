"""SKYSCANNER connect/read timeouts are read from the environment at import time.

`app.SKYSCANNER_CONNECT_TIMEOUT` / `app.SKYSCANNER_SEARCH_TIMEOUT` are resolved when
the module is imported, so these run `import app` in a subprocess with a controlled
env. The subprocess neutralizes `dotenv.load_dotenv` BEFORE importing app, so the
result depends only on the passed env vars and never on any `.env` on the developer's
machine. Mirrors test_port.py.
"""
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_SCRIPT = (
    "import dotenv; dotenv.load_dotenv = lambda *a, **k: None; "
    "import app; "
    "print(app.SKYSCANNER_CONNECT_TIMEOUT); print(app.SKYSCANNER_SEARCH_TIMEOUT)"
)
_TIMEOUT_VARS = ("SKYSCANNER_CONNECT_TIMEOUT", "SKYSCANNER_SEARCH_TIMEOUT")


def _import_timeouts(env_overrides):
    """Return (connect, read) as the strings app prints under the given env."""
    env = {k: v for k, v in os.environ.items() if k not in _TIMEOUT_VARS}
    env["PYTHONPATH"] = _REPO
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    connect, read = result.stdout.split()
    return connect, read


def test_timeouts_default_when_unset():
    # Connect 10s bounds connection setup so a dead host fails fast. The search read
    # timeout is an INACTIVITY timeout (seconds of NO data before giving up), not a
    # total cap; 90 is generous so a slow-but-progressing async search completes
    # instead of being killed mid-flight.
    assert _import_timeouts({}) == ("10", "90")


def test_connect_timeout_respects_env_override():
    connect, _read = _import_timeouts({"SKYSCANNER_CONNECT_TIMEOUT": "3"})
    assert connect == "3"


def test_search_timeout_respects_env_override():
    _connect, read = _import_timeouts({"SKYSCANNER_SEARCH_TIMEOUT": "120"})
    assert read == "120"
