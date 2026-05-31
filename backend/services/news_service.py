from typing import Optional, List
from datetime import datetime, timedelta
from .auth import alpaca_request_async

async def get_news(symbols: List[str], start: Optional[datetime] = None, end: Optional[datetime] = None, limit: int = 50) -> List[dict]:
    try:
        if start is None:
            start = datetime.utcnow() - timedelta(days=7)
        if end is None:
            end = datetime.utcnow()
        params = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit,
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        response = await alpaca_request_async("GET", "/v1beta1/news", params=params, use_data_api=True)
        if response.status_code == 401:
            print("ERROR: 401 Unauthorized from Alpaca news API.")
            return []
        response.raise_for_status()
        return response.json().get("news", [])
    except Exception as e:
        print(f"Error fetching news: {e}")
        return []

async def get_latest_news(symbol: str, limit: int = 5) -> List[dict]:
    return await get_news([symbol], limit=limit)

async def get_news_sentiment(symbol: str, limit: int = 10) -> dict:
    articles = await get_latest_news(symbol, limit=limit)
    if not articles:
        return {"symbol": symbol, "sentiment_score": 0.0, "sentiment_label": "neutral", "article_count": 0, "recent_articles": []}

    positive_words = {"bull", "gain", "rise", "up", "buy", "outperform", "beat", "strong", "growth", "profit", "record"}
    negative_words = {"bearish", "loss", "fall", "down", "sell", "underperform", "miss", "weak", "decline", "drop", "crash"}

    positive_count = sum(1 for a in articles if any(w in (a.get("headline", "") + a.get("summary", "")).lower() for w in positive_words))
    negative_count = sum(1 for a in articles if any(w in (a.get("headline", "") + a.get("summary", "")).lower() for w in negative_words))
    total = positive_count + negative_count
    sentiment_score = (positive_count - negative_count) / total if total > 0 else 0.0
    sentiment_label = "positive" if sentiment_score > 0.1 else "negative" if sentiment_score < -0.1 else "neutral"

    return {
        "symbol": symbol,
        "sentiment_score": round(sentiment_score, 3),
        "sentiment_label": sentiment_label,
        "article_count": len(articles),
        "recent_articles": articles[:5],
    }