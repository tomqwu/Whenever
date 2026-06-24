"""The dev-server PORT is read from the environment at import time.

`app.PORT` is resolved when the module is imported, so these run `import app` in
a subprocess with a controlled env. The subprocess neutralizes
`dotenv.load_dotenv` BEFORE importing app, so the result depends only on the
passed env vars and never on any `.env` on the developer's machine.
"""
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
# Stub out dotenv so app's load_dotenv() can't pull in a repo-local .env.
_SCRIPT = "import dotenv; dotenv.load_dotenv = lambda *a, **k: None; import app; print(app.PORT)"


def _import_app_port(env_overrides):
    env = {k: v for k, v in os.environ.items() if k != "PORT"}
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
    return result.stdout.strip()


def test_port_defaults_to_5001_when_unset():
    # Default avoids macOS AirPlay Receiver, which holds port 5000.
    assert _import_app_port({}) == "5001"


def test_port_respects_env_override():
    assert _import_app_port({"PORT": "8123"}) == "8123"
