import sys
from pathlib import Path

# The project is a single top-level script, not an installed package.
sys.path.insert(0, str(Path(__file__).parent.parent))
