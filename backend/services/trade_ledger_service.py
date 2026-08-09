from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import json

from backend import config as _config
_DATA_DIR = Path(getattr(_config, "DATA_DIR", "."))
LEDGER_PATH = _DATA_DIR / "trade_ledger.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def load_ledger() -> dict[str, list[dict[str, Any]]]:
    if not LEDGER_PATH.exists():
        return {}
    try:
        with LEDGER_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ledger(ledger: dict[str, list[dict[str, Any]]]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, default=str)


def _latest_entry(ledger: dict[str, list[dict[str, Any]]], symbol: str) -> dict[str, Any] | None:
    entries = ledger.get(symbol, [])
    if not isinstance(entries, list) or not entries:
        return None
    return entries[-1]


def is_in_cooldown(
    ledger: dict[str, list[dict[str, Any]]],
    symbol: str,
) -> tuple[bool, datetime | None]:
    entry = _latest_entry(ledger, symbol)
    if not entry:
        return False, None

    cooldown_until = _from_iso(entry.get("cooldown_until"))
    if cooldown_until is None:
        return False, None

    now = _now_utc()
    return now < cooldown_until, cooldown_until


def add_entry(
    ledger: dict[str, list[dict[str, Any]]],
    symbol: str,
    side: str,
    qty: float,
    entry_price: float | None,
    order_id: str | None,
    strategy: str,
    metadata: dict[str, Any] | None = None,
    cooldown_minutes: int = 20,
) -> dict[str, Any]:
    now = _now_utc()
    entry = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry_price": entry_price,
        "order_id": order_id,
        "strategy": strategy,
        "status": "open",
        "created_at": _to_iso(now),
        "last_action_at": _to_iso(now),
        "cooldown_until": _to_iso(now + timedelta(minutes=cooldown_minutes)),
        "metadata": metadata or {},
    }
    ledger.setdefault(symbol, []).append(entry)
    return entry


def close_entry(
    ledger: dict[str, list[dict[str, Any]]],
    symbol: str,
    order_id: str | None = None,
    exit_price: float | None = None,
    reason: str = "closed",
    cooldown_minutes: int = 20,
) -> dict[str, Any] | None:
    entries = ledger.get(symbol, [])
    if not isinstance(entries, list) or not entries:
        return None

    for entry in reversed(entries):
        if entry.get("status") == "open":
            now = _now_utc()
            entry["status"] = "closed"
            entry["exit_price"] = exit_price
            entry["close_order_id"] = order_id
            entry["close_reason"] = reason
            entry["closed_at"] = _to_iso(now)
            entry["last_action_at"] = _to_iso(now)
            entry["cooldown_until"] = _to_iso(now + timedelta(minutes=cooldown_minutes))
            return entry

    return None
