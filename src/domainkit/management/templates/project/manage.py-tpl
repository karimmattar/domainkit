"""Run the project's commands: python manage.py <command>."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    # Find pyproject.toml and .env whatever the working directory.
    os.environ.setdefault("DOMAINKIT_PROJECT_ROOT", str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    from domainkit.management import execute_from_command_line

    execute_from_command_line(sys.argv)
