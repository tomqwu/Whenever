import os
import subprocess
import sys


def test_dotenv_loads_token(tmp_path):
    (tmp_path / ".env").write_text("TRAVELPAYOUTS_TOKEN=dotenv-test-token\n")

    env = {k: v for k, v in os.environ.items() if k != "TRAVELPAYOUTS_TOKEN"}
    env["PYTHONPATH"] = str(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    result = subprocess.run(
        [sys.executable, "-c", "import app; print(app.TRAVELPAYOUTS_TOKEN)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dotenv-test-token"
