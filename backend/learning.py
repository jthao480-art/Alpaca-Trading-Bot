"""
learning.py – Monday retraining / recalibration loop.
Loads closed trades, retrains the model, saves artifact, notifies event bus.
"""
from __future__ import annotations
import logging
import os
import uuid
from datetime import datetime
from typing import Optional
from xml.parsers.expat import model

import joblib
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import cross_val_score

from . import config
from . import event_bus as eb
from .db.trades_repo import get_closed_trades, insert_learning_run
from .schemas import LearningSummary
from .services.model_service import model_service

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 20   # don't retrain on tiny datasets


async def run_learning_job() -> None:
    """Execute the full Monday retraining pipeline."""
    logger.info("Learning job started.")
    try:
        trades = await get_closed_trades(limit=2000)
        if len(trades) < _MIN_SAMPLES:
            logger.info(
                "Learning job: only %d closed trades – skipping (min %d).",
                len(trades), _MIN_SAMPLES,
            )
            return

        X, y = _build_dataset(trades)
        if X is None or len(X) < _MIN_SAMPLES:
            logger.info("Learning job: insufficient feature data – skipping.")
            return

        model = GradientBoostingClassifier(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.1,
            random_state=42,
        )

        # Cross-validation accuracy
        scores = cross_val_score(model, X, y, cv=min(5, len(X) // 10 or 2))
        accuracy = float(np.mean(scores))

        if y is None:
            raise ValueError("Training labels are missing")
        model.fit(X, y)

        # Save model
        os.makedirs(os.path.dirname(config.MODEL_PATH) or ".", exist_ok=True)
        joblib.dump(model, config.MODEL_PATH)
        logger.info("Learning job: model saved to %s  accuracy=%.3f", config.MODEL_PATH, accuracy)

        # Reload live model service
        model_service.reload()
        version = model_service.version

        summary = LearningSummary(
            model_version=version,
            n_samples=len(X),
            accuracy=accuracy,
            notes=f"Retrained on {len(trades)} trades; CV accuracy={accuracy:.3f}",
        )
        await insert_learning_run(summary)
        await eb.bus.publish(
            eb.TOPIC_LEARNING_SUMMARY,
            summary.model_dump(mode="json"),
        )
        logger.info("Learning job complete: version=%s accuracy=%.3f", version, accuracy)

    except Exception:
        logger.exception("Learning job failed.")


def _build_dataset(trades):
    """
    Build X (feature matrix) and y (labels) from closed trades.
    Label = 1 if pnl > 0, else 0.
    """
    feature_keys = [
        "news_score", "wallet_score", "momentum_score",
        "volume_score", "forecast_score", "fundamentals_score",
    ]
    rows, labels = [], []
    for t in trades:
        if t.pnl is None:
            continue
        feats = t.features
        row = [feats.get(k, 0.5) for k in feature_keys]
        rows.append(row)
        labels.append(1 if t.pnl > 0 else 0)

    if not rows:
        return None, None

    return np.array(rows, dtype=float), np.array(labels, dtype=int)

