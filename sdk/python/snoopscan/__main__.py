"""`python -m snoopscan`: the CLI when the `snoopscan` command is not on the PATH.

A `pip install --user` puts the command in a directory many shells do not
search, and "command not found" right after a successful install is the most
common way a first run fails. The module form needs only the interpreter.
"""

from snoopscan.cli import main

raise SystemExit(main())
