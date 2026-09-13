import logging
from decimal import Decimal

from domain.models import MarketState

logger = logging.getLogger("blessing.engines.ml_scorer")

class GridSafetyScorerML:
    """
    Research-only model boundary.

    A model is deliberately not part of the live worker path.  Until a real,
    versioned research model and its out-of-sample evidence are supplied, this
    compatibility class fails closed instead of returning a fabricated score.
    """
    def __init__(self, model_path: str = None):
        self.model_path = model_path
        self.is_loaded = False
        self.model = None

    def load_model(self):
        """Reject model use until a separately verified loader is integrated."""
        raise RuntimeError(
            "Research ML scorer is unavailable: no verified model loader/evidence is configured"
        )

    def predict_safety_score(self, market_state: MarketState, pa_state) -> Decimal:
        """
        Extracts features from MarketState and PriceAction, and predicts 
        the probability of profitable grid mean-reversion within the next N hours.
        """
        raise RuntimeError(
            "Research ML scorer cannot produce a score without verified out-of-sample model evidence"
        )
