from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config

MODEL_PATH = Path(getattr(config, "MODEL_PATH", "models/model.pkl"))


def load_model() -> Any:
    return None


def model_exists() -> bool:
    return MODEL_PATH.exists()
