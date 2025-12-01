import sys
from pathlib import Path

# Ensure project root is on PYTHONPATH so `flask_app` resolves when run as a script.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask_app.app.scoring import run_scoring_workflow


if __name__ == "__main__":
    print(run_scoring_workflow())
