import sys
from pathlib import Path

# Make the repo root importable regardless of pytest's start directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
