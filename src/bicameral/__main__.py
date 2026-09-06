"""`python -m bicameral` does the same as the `bicameral` command.

Useful when pip put the `bicameral` script somewhere that is not on PATH,
which is common on Windows.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
