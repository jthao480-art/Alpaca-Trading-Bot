"""
agents/volume_agent.py – Watches liquidity and volume spikes.
"""
from __future__ import annotations
from typing import Optional

import numpy as np

from .base import BaseAgent
from ..schemas import AgentSignal
from ..services.bars_service import get_bars
from .. import config


class VolumeAgent(BaseAgent):
    name = "volume"

    async def analyze(self, symbol: str) -> Optional[AgentSignal]:
        try:
            bars = await get_bars(symbol, timeframe="5Min", limit=40) or []
            if len(bars) < 10:
             return self._make_signal(symbol, 0.5, "hold", 0.3, "insufficient bars")

            volumes = [float(b.get("v", 0)) for b in bars]
            avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 1.0
            current_vol = volumes[-1]

            if avg_vol == 0:
                return self._make_signal(symbol, 0.5, "hold", 0.3, "zero avg volume")

            ratio = current_vol / avg_vol
            min_volume = getattr(config, "MIN_VOLUME", 100000)

            if current_vol < min_volume:
                return self._make_signal(
                    symbol, 0.3, "hold", 0.9,
                    f"volume {current_vol:.0f} below min {min_volume}",
                    volume=current_vol,
                )

            score = min(ratio / 2, 1.0)
            direction = "buy" if score > 0.6 else ("sell" if score < 0.35 else "hold")

            return self._make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=0.7,
                reason=f"vol_ratio={ratio:.2f} current={current_vol:.0f}",
                vol_ratio=round(ratio, 2),
                current_volume=current_vol,
                avg_volume=round(avg_vol, 0),
            )
        except Exception:
            self.logger.exception("VolumeAgent failed for %s", symbol)
            return None