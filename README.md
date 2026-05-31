# TradingBot v3 – Autonomous Event-Driven Trading System

A full-stack, modular, event-driven trading bot built for the **Alpaca** platform.
Uses a weighted-confidence scoring system (no hard consensus vote) with a React
real-time dashboard powered by WebSocket events.

---

## Quick Start (One Command)

### macOS / Linux
```bash
# 1. Clone / extract the project
# 2. Run:
chmod +x start.sh
./start.sh
```

### Windows
```bat
Double-click start.bat
```

The script will:
1. Copy `.env.example` → `.env` on first run (edit your Alpaca keys)
2. Create a Python virtualenv and install backend deps
3. Install frontend Node dependencies
4. Launch backend (FastAPI + WebSocket) and frontend (Next.js) concurrently

---

## Project Structure

```
trading_system/
├── backend/
│   ├── botv3.py              ← Single entry point
│   ├── config.py             ← All settings via env vars
│   ├── event_bus.py          ← Async pub/sub bus
│   ├── schemas.py            ← Pydantic data models
│   ├── orchestrator.py       ← Coordinator + weighted scoring
│   ├── execution.py          ← Alpaca bracket order placement
│   ├── state_store.py        ← Runtime state (JSON-backed)
│   ├── learning.py           ← Monday retraining loop
│   ├── ws_bridge.py          ← WebSocket → frontend bridge
│   ├── api.py                ← FastAPI REST endpoints
│   ├── agents/
│   │   ├── news_agent.py
│   │   ├── wallet_agent.py
│   │   ├── momentum_agent.py
│   │   ├── volume_agent.py
│   │   ├── forecast_agent.py
│   │   ├── fundamentals_agent.py
│   │   └── risk_agent.py     ← Hard veto (never disabled)
│   ├── services/
│   │   ├── bars_service.py
│   │   ├── news_service.py
│   │   ├── wallet_service.py
│   │   ├── fundamentals_service.py
│   │   └── model_service.py  ← Central ML model I/O
│   └── db/
│       ├── init_db.py
│       ├── migrations.py
│       └── trades_repo.py
├── frontend/
│   ├── hooks/useEventStream.ts    ← WebSocket consumer
│   ├── lib/tradingStore.ts        ← State reducer
│   ├── components/
│   │   ├── AgentPanel.tsx
│   │   ├── DecisionFeed.tsx
│   │   ├── PositionsTable.tsx
│   │   ├── TradeHistory.tsx
│   │   ├── PnLChart.tsx
│   │   ├── AlertBanner.tsx
│   │   └── StatusBar.tsx
│   └── pages/index.tsx            ← Dashboard
├── output/                        ← DB, state, model (auto-created)
├── .env.example
├── start.sh
├── start.bat
└── docker-compose.yml
```

---

## Configuration

Copy `.env.example` → `.env` and set your values:

| Variable | Default | Description |
|---|---|---|
| `ALPACA_API_KEY` | — | **Required** |
| `ALPACA_SECRET_KEY` | — | **Required** |
| `ALPACA_BASE_URL` | paper API | Switch to live URL for real money |
| `SYMBOLS` | AAPL,MSFT,TSLA,NVDA,AMD | Comma-separated watchlist |
| `BUY_THRESHOLD` | 0.58 | Weighted score to trigger buy |
| `SELL_THRESHOLD` | 0.46 | Weighted score to trigger sell/exit |
| `MAX_POSITION_SIZE_USD` | 1000 | Max dollars per position |
| `DAILY_LOSS_LIMIT_USD` | 500 | Halt trading after this loss |
| `SCAN_INTERVAL_SECONDS` | 60 | How often to run agent scans |

---

## Agent Weights

| Agent | Weight | Role |
|---|---|---|
| momentum | 0.25 | RSI + EMA crossover |
| forecast | 0.25 | ML model prediction |
| volume | 0.20 | Liquidity + spike detection |
| news | 0.10 | Headline sentiment |
| wallet | 0.10 | Portfolio health |
| fundamentals | 0.10 | Daily price change context |
| **risk** | **VETO** | Hard safety rules – always active |

---

## Scoring Logic

```
weighted_score = Σ(agent_score × agent_weight) / Σ(weights)

if weighted_score >= 0.58  →  BUY
if weighted_score <= 0.46  →  SELL / EXIT
otherwise                  →  HOLD
```

---

## Architecture

```
Agents ──► Orchestrator ──► EventBus ──► WebSocket Bridge ──► Frontend
                │                              │
                ▼                              ▼
           Execution                      REST API
           (Alpaca)                   (FastAPI /state)
                │
                ▼
            Database
          (SQLite / trades.db)
```

**Backend is the source of truth.** The frontend is a pure event consumer – it
never drives trade logic.

---

## Endpoints

| URL | Description |
|---|---|
| `http://localhost:8000/health` | Health check |
| `http://localhost:8000/state` | Current state snapshot |
| `http://localhost:8000/trades/open` | Open positions |
| `http://localhost:8000/trades/closed` | Closed trade history |
| `ws://localhost:8765` | Live event stream |
| `http://localhost:3000` | Dashboard UI |

---

## Monday Retraining

Every Monday at 13:30 UTC (≈ 9:30 ET) before market open, the bot:

1. Loads all closed trades from the database
2. Builds a feature matrix (agent scores → outcome label)
3. Trains a `GradientBoostingClassifier` (requires ≥ 20 samples)
4. Saves model to `output/model.joblib`
5. Hot-reloads the live `ModelService`
6. Saves a `LearningSummary` to the database
7. Publishes a `learning.summary` event to the dashboard

---

## Docker (Production)

```bash
cp .env.example .env
# Edit .env with your credentials
docker-compose up --build
```

---

## Safety Rails (Never Disabled)

- Daily loss limit halts all trading
- Spread > 0.5% blocks buys
- Duplicate position prevention
- Insufficient buying power check
- Minimum volume liquidity gate
- Bracket orders (take-profit + stop-loss on every trade)
- Retry logic with backoff on API failures
- State persisted to disk for crash recovery

---

## Requirements

- Python 3.10+
- Node.js 18+ (for frontend)
- Alpaca account (paper or live)
