"""Make lrc_studio's flat, script-style imports (``from constants import ...``)
work under pytest, matching how main.py finds them when run directly."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lrc_studio"))
