from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    # Keep every temporary file inside the project (out/ is git-ignored), not in the system temp dir.
    # A fresh clone has no out/ yet, and pytest only creates the basetemp itself, not its parents.
    if not config.option.basetemp:
        base = ROOT / "out" / ".pytest-tmp"
        base.parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(base)
