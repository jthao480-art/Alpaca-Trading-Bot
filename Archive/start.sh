#!/usr/bin/env bash
# =============================================================
# start.sh – One-command launcher for TradingBot v3
# Usage:  chmod +x start.sh && ./start.sh
# =============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
OUTPUT_DIR="$SCRIPT_DIR/output"
ENV_FILE="$SCRIPT_DIR/.env"

# ─────────────────────────────────────────
# Colours
# ─────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[TradingBot]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─────────────────────────────────────────
# 1. Env file
# ─────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  warn ".env not found – copying from .env.example"
  cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
  warn "Please edit $ENV_FILE with your Alpaca credentials, then re-run."
  exit 1
fi

# Export env vars into this shell session
set -o allexport
source "$ENV_FILE"
set +o allexport

# ─────────────────────────────────────────
# 2. Output directory
# ─────────────────────────────────────────
mkdir -p "$OUTPUT_DIR"
ok "Output directory ready: $OUTPUT_DIR"

# ─────────────────────────────────────────
# 3. Python / backend deps
# ─────────────────────────────────────────
log "Checking Python …"
if ! command -v python3 &>/dev/null; then
  err "python3 not found. Please install Python 3.10+."
fi
PYTHON=$(command -v python3)
ok "Python: $($PYTHON --version)"

VENV="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV" ]; then
  log "Creating virtualenv …"
  $PYTHON -m venv "$VENV"
fi
source "$VENV/bin/activate"

log "Installing backend dependencies …"
pip install -q --upgrade pip
pip install -q -r "$BACKEND_DIR/requirements.txt"
ok "Backend dependencies installed."

# ─────────────────────────────────────────
# 4. Node / frontend deps
# ─────────────────────────────────────────
log "Checking Node …"
if ! command -v node &>/dev/null; then
  warn "Node.js not found – frontend will not start."
  SKIP_FRONTEND=1
else
  ok "Node: $(node --version)"
  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    log "Installing frontend dependencies …"
    cd "$FRONTEND_DIR"
    npm install --silent
    cd "$SCRIPT_DIR"
  fi
  ok "Frontend dependencies ready."

  # Copy .env.local if missing
  if [ ! -f "$FRONTEND_DIR/.env.local" ]; then
    cp "$FRONTEND_DIR/.env.local.example" "$FRONTEND_DIR/.env.local" 2>/dev/null || true
  fi
fi

# ─────────────────────────────────────────
# 5. Launch
# ─────────────────────────────────────────
log "Starting TradingBot v3 …"
echo ""
echo "  Backend API  → http://localhost:${API_PORT:-8000}"
echo "  WebSocket    → ws://localhost:${WS_PORT:-8765}"
if [ -z "$SKIP_FRONTEND" ]; then
  echo "  Frontend UI  → http://localhost:3000"
fi
echo ""

# Trap for clean shutdown
cleanup() {
  log "Shutting down …"
  kill "$BACKEND_PID" 2>/dev/null || true
  kill "$FRONTEND_PID" 2>/dev/null || true
  wait
  ok "All processes stopped."
}
trap cleanup INT TERM EXIT

# Start backend
cd "$BACKEND_DIR"
python botv3.py &
BACKEND_PID=$!
ok "Backend started (PID $BACKEND_PID)"

# Start frontend
if [ -z "$SKIP_FRONTEND" ]; then
  cd "$FRONTEND_DIR"
  npm run dev &
  FRONTEND_PID=$!
  ok "Frontend started (PID $FRONTEND_PID)"
fi

# Wait forever (until Ctrl+C)
wait
