"""Entry point for a cloned checkout: `python main.py --all --fresh`.

The pipeline is a package, and its modules import each other with relative
imports, so it has to be run as `python -m publish_pipeline.run_scenario1`
from the directory *above* the checkout. That is an easy thing to get wrong on
a fresh clone, and the failure it produces ("attempted relative import with no
known parent package") does not point at the fix. This script removes the
question: it puts the parent directory on sys.path and hands over to the real
entry point, so the command works from inside the checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit(
        f"Python {sys.version_info.major}.{sys.version_info.minor} found, 3.10+ required.\n"
        "macOS ships 3.9 as the system python3. Create a virtualenv with a newer\n"
        "interpreter first:\n\n"
        "    python3.11 -m venv .venv\n"
        "    .venv/bin/pip install -r requirements.txt\n"
        "    .venv/bin/python main.py --all --fresh\n"
    )

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    from publish_pipeline.run_scenario1 import main
except ImportError as exc:  # the checkout was renamed, so the package name no longer matches
    sys.exit(
        f"Could not import the publish_pipeline package ({exc}).\n"
        f"This directory is named {HERE.name!r}; it must be named 'publish_pipeline'\n"
        "for the package's relative imports to resolve."
    )

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
