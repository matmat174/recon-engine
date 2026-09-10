"""recon-engine — transaction reconciliation with explainable break classification."""

from .config import DEFAULT_CONFIG, MatchConfig
from .engine import reconcile
from .generate import build_dataset
from .metrics import Metrics, evaluate
from .models import Break, Dataset, Match, ReconResult, Transaction, TruthGroup

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_CONFIG",
    "MatchConfig",
    "reconcile",
    "build_dataset",
    "evaluate",
    "Metrics",
    "Break",
    "Dataset",
    "Match",
    "ReconResult",
    "Transaction",
    "TruthGroup",
    "__version__",
]
