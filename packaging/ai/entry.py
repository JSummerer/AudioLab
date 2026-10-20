"""Dedicated frozen entry for the optional offline AI runtime."""

import sys

# Set the role before importing AudioLab: its frozen core and this helper differ.
sys._audiolab_ai_worker = True

from audiolab.ai.worker import main  # noqa: E402

raise SystemExit(main())
