"""Test package bootstrap: put the repo root on sys.path.

Lets the suite run from anywhere, with unittest or pytest, without an
install step or a PYTHONPATH dance.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
