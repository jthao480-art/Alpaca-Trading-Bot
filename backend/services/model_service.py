"""
services/model_service.py – Central model loading, inference, and reloading.
All model I/O must go through this module.
"""
from __future__ import annotations
import hashlib
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

import joblib
import numpy as np

from .. import config

logger = logging.getLogger(__name__)


class ModelService:
    def __init__(self) -> None:
        self._model: Optional[Any] = None
        self._version: str = "none"
        self._loaded_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------
    def load(self) -> bool:
        path = config.MODEL_PATH
        if not os.path.exists(path):
            logger.warning("ModelService: no model at %s; will use fallback.", path)
            return False
        try:
            self._model = joblib.load(path)
            self._loaded_at = datetime.utcnow()
            self._version = _file_hash(path)[:8]
            logger.info("ModelService: loaded model %s from %s", self._version, path)
            return True
        except Exception:
            logger.exception("ModelService: failed to load model from %s", path)
            return False

    def reload(self) -> bool:
        logger.info("ModelService: reloading …")
        return self.load()

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------
    def predict(self, features: Dict[str, float]) -> float:
        """
        Return a probability 0..1 of a positive (buy) outcome.
        Falls back to a simple heuristic if no model is loaded.
        """
        if self._model is None:
            return self._heuristic_fallback(features)
        try:
            X = np.array([[features.get(k, 0.0) for k in _FEATURE_KEYS]])
            proba = self._model.predict_proba(X)[0][1]
            return float(proba)
        except Exception:
            logger.exception("ModelService: inference failed; using fallback")
            return self._heuristic_fallback(features)

    @staticmethod
    def _heuristic_fallback(features: Dict[str, float]) -> float:
        """
        Simple average of available scores as a stand-in until the model
        is trained with real data.
        """
        vals = [v for v in features.values() if isinstance(v, (int, float))]
        return float(np.mean(vals)) if vals else 0.5

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    @property
    def version(self) -> str:
        return self._version

    @property
    def loaded_at(self) -> Optional[datetime]:
        return self._loaded_at

    @property
    def is_loaded(self) -> bool:
        return self._model is not None


# ---------------------------------------------------------------------------
# Feature key order must match training code in orchestrator.py
# ---------------------------------------------------------------------------
_FEATURE_KEYS = [
    "news_score", "wallet_score", "momentum_score",
    "volume_score", "forecast_score", "fundamentals_score",
]


def _file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# Module-level singleton
model_service: ModelService = ModelService()

