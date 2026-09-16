"""Allow ``python -m apps.release_gate.repo_gate`` invocation."""

import sys

from apps.release_gate.repo_gate import main

sys.exit(main())
