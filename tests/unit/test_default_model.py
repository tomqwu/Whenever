"""Verify the shipped default for OLLAMA_MODEL is qwen3:8b.

Uses the same subprocess+stubbed-dotenv pattern as test_port.py so the
result is env-isolated: the repo .env (if present) cannot influence the
default that is baked into app.py.
"""
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
# Stub dotenv so app's load_dotenv() can't pull in a repo-local .env file.
_SCRIPT = (
    "import dotenv; dotenv.load_dotenv = lambda *a, **k: None; "
    "import app; print(app.OLLAMA_MODEL)"
)


def _import_app_ollama_model(env_overrides):
    env = {k: v for k, v in os.environ.items() if k != "OLLAMA_MODEL"}
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


def test_ollama_model_defaults_to_qwen3_8b():
    """When OLLAMA_MODEL is unset and .env is neutralised, the default is qwen3:8b."""
    assert _import_app_ollama_model({}) == "qwen3:8b"


def test_ollama_model_respects_env_override():
    """OLLAMA_MODEL env var overrides the baked-in default."""
    assert _import_app_ollama_model({"OLLAMA_MODEL": "llama3.1:8b"}) == "llama3.1:8b"
