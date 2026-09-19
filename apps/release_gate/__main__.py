"""Allow ``python -m apps.release_gate`` invocation."""

import sys

from apps.release_gate.gate import main

sys.exit(main())
