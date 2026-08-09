from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LearningEvent:
    timestamp: str
    symbol: str
    action: str
    score: float = 0.0
    confidence: float = 0.0
    status: str = ""
    pnl: float = 0.0
    meta: Optional[Dict[str, Any]] = None


class Learning:
    def __init__(self, storage_path: str = "backend/db/learning_events.jsonl"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(
        self,
        symbol: str,
        action: str,
        score: float = 0.0,
        confidence: float = 0.0,
        status: str = "",
        pnl: float = 0.0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> LearningEvent:
        event = LearningEvent(
            timestamp=datetime.utcnow().isoformat(),
            symbol=symbol,
            action=action,
            score=float(score),
            confidence=float(confidence),
            status=status,
            pnl=float(pnl),
            meta=meta or {},
        )
        with self.storage_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event)) + "\n")
        return event

    def load_events(self, limit: int = 1000) -> List[Dict[str, Any]]:
        if not self.storage_path.exists():
            return []
        rows = []
        with self.storage_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        return rows[-limit:]

    def summarize(self, limit: int = 1000) -> Dict[str, Any]:
        events = self.load_events(limit=limit)
        total = len(events)
        buys = sum(1 for e in events if e.get("action") == "buy")
        sells = sum(1 for e in events if e.get("action") == "sell")
        holds = sum(1 for e in events if e.get("action") == "hold")
        pnl = sum(float(e.get("pnl", 0) or 0) for e in events)
        return {
            "total_events": total,
            "buys": buys,
            "sells": sells,
            "holds": holds,
            "net_pnl": round(pnl, 2),
        }

    def update_from_run(self, run_result: Dict[str, Any]) -> int:
        results = run_result.get("results", []) if isinstance(run_result, dict) else []
        count = 0
        for r in results:
            symbol = r.get("symbol", "")
            action = r.get("action", r.get("status", ""))
            if not symbol:
                continue
            self.log_event(
                symbol=symbol,
                action=action,
                score=r.get("score", 0.0),
                confidence=r.get("confidence", 0.0),
                status=r.get("status", ""),
                meta=r,
            )
            count += 1
        return count
