"""``python -m domainkit``: the ``domainkit`` program, without its script.

The console script is the usual way in; this one serves where scripts are
not on the ``PATH``, such as an interpreter run without activating its
virtual environment. It dispatches as the script does, under its own name.
"""

import sys

from domainkit.management import execute_from_command_line

PROG = "python -m domainkit"
"""The program's name in usage lines and errors."""

if __name__ == "__main__":
    execute_from_command_line([PROG, *sys.argv[1:]])
