from typing import Optional
from .auth import alpaca_request_async

async def get_symbol_summary(symbol: str) -> Optional[dict]:
    try:
        response = await alpaca_request_async("GET", f"/v1beta1/stocks/{symbol}/stats", use_data_api=True)
        if response.status_code in [401, 404]: return None
        response.raise_for_status()
        return response.json().get("stats")
    except Exception as e:
        print(f"Error: {e}")
        return None

async def get_snapshot(symbol: str) -> Optional[dict]:
    try:
        response = await alpaca_request_async("GET", f"/v1beta1/stocks/{symbol}/snapshot", use_data_api=True)
        if response.status_code in [401, 404]: return None
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error: {e}")
        return None

async def get_market_cap(symbol: str) -> Optional[float]:
    s = await get_symbol_summary(symbol)
    return s.get("market_cap") if s else None

async def get_pe_ratio(symbol: str) -> Optional[float]:
    s = await get_symbol_summary(symbol)
    return s.get("pe_ratio") if s else None

async def score_fundamentals(symbol: str) -> dict:
    try:
        summary = await get_symbol_summary(symbol)
        if not summary: return {"symbol": symbol, "score": 50.0, "label": "hold", "pe_ratio": None, "market_cap": None, "price_to_book": None, "revenue_ttm": None}
        pe, mc, pb = summary.get("pe_ratio"), summary.get("market_cap"), summary.get("price_to_book")
        score = 50.0
        if pe: score += 15 if 10 <= pe <= 25 else 10 if pe < 10 else -10 if pe > 30 else 0
        if mc: score += 10 if mc > 10e9 else 5 if mc > 2e9 else -10 if mc < 300e6 else 0
        if pb: score += 10 if 1 <= pb <= 3 else 5 if pb < 1 else -5 if pb > 5 else 0
        score = max(0, min(100, score))
        label = "strong_buy" if score >= 80 else "buy" if score >= 65 else "hold" if score >= 40 else "sell" if score >= 20 else "strong_sell"
        return {"symbol": symbol, "score": round(score, 1), "label": label, "pe_ratio": pe, "market_cap": mc, "price_to_book": pb, "revenue_ttm": summary.get("revenue_ttm")}
    except Exception as e:
        print(f"Error: {e}")
        return {"symbol": symbol, "score": 50.0, "label": "hold", "pe_ratio": None, "market_cap": None, "price_to_book": None, "revenue_ttm": None}