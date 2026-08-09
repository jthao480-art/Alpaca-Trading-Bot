// lib/tradingStore.ts
// Pure React state reducer – no external library needed.
import { useReducer, useCallback } from "react";

export type AgentSignal = {
  agent: string;
  symbol: string;
  score: number;
  direction: "buy" | "sell" | "hold";
  confidence: number;
  reason: string;
  timestamp: string;
  metadata?: Record<string, unknown>;
};

export type Decision = {
  symbol: string;
  action: "buy" | "sell" | "hold";
  weighted_score: number;
  veto: boolean;
  veto_reason: string;
  signals: AgentSignal[];
  timestamp: string;
};

export type TradeRecord = {
  id?: number;
  symbol: string;
  action: string;
  qty: number;
  entry_price: number;
  exit_price?: number;
  take_profit: number;
  stop_loss: number;
  status: "open" | "closed" | "cancelled";
  pnl?: number;
  entry_ts: string;
  exit_ts?: string;
};

export type LearningSummary = {
  model_version: string;
  trained_at: string;
  n_samples: number;
  accuracy?: number;
  notes: string;
};

export type TradingState = {
  connected: boolean;
  signals: AgentSignal[];           // last signal per agent per symbol
  decisions: Decision[];            // recent decisions (cap 50)
  openPositions: Record<string, TradeRecord>;
  closedTrades: TradeRecord[];      // cap 100
  alerts: string[];                 // cap 20
  learningSummaries: LearningSummary[];
  dailyPnl: number;
  dailyLossHit: boolean;
  modelVersion: string;
  lastHeartbeat: string | null;
};

type Action =
  | { type: "SET_CONNECTED"; payload: boolean }
  | { type: "AGENT_SIGNAL"; payload: AgentSignal }
  | { type: "COORDINATOR_DECISION"; payload: Decision }
  | { type: "TRADE_OPENED"; payload: TradeRecord }
  | { type: "TRADE_CLOSED"; payload: TradeRecord }
  | { type: "STATE_SNAPSHOT"; payload: Record<string, unknown> }
  | { type: "LEARNING_SUMMARY"; payload: LearningSummary }
  | { type: "RISK_VETO"; payload: { symbol: string; reason: string } }
  | { type: "HEARTBEAT"; payload: { ts: string } }
  | { type: "ERROR"; payload: { symbol?: string; error: string } };

const initialState: TradingState = {
  connected: false,
  signals: [],
  decisions: [],
  openPositions: {},
  closedTrades: [],
  alerts: [],
  learningSummaries: [],
  dailyPnl: 0,
  dailyLossHit: false,
  modelVersion: "unknown",
  lastHeartbeat: null,
};

function reducer(state: TradingState, action: Action): TradingState {
  switch (action.type) {
    case "SET_CONNECTED":
      return { ...state, connected: action.payload };

    case "AGENT_SIGNAL": {
      const sig = action.payload;
      const existing = state.signals.filter(
        (s) => !(s.agent === sig.agent && s.symbol === sig.symbol)
      );
      return { ...state, signals: [...existing, sig] };
    }

    case "COORDINATOR_DECISION": {
      const d = action.payload;
      const updated = [d, ...state.decisions].slice(0, 50);
      const alerts =
        d.veto
          ? [`VETO ${d.symbol}: ${d.veto_reason}`, ...state.alerts].slice(0, 20)
          : d.action !== "hold"
          ? [
              `${d.action.toUpperCase()} signal for ${d.symbol} (score ${d.weighted_score.toFixed(2)})`,
              ...state.alerts,
            ].slice(0, 20)
          : state.alerts;
      return { ...state, decisions: updated, alerts };
    }

    case "TRADE_OPENED": {
      const t = action.payload;
      const positions = { ...state.openPositions, [t.symbol]: t };
      const alerts = [
        `Opened ${t.action.toUpperCase()} ${t.qty} ${t.symbol} @ $${t.entry_price}`,
        ...state.alerts,
      ].slice(0, 20);
      return { ...state, openPositions: positions, alerts };
    }

    case "TRADE_CLOSED": {
      const t = action.payload;
      const positions = { ...state.openPositions };
      delete positions[t.symbol];
      const closed = [t, ...state.closedTrades].slice(0, 100);
      const pnlStr = t.pnl !== undefined ? ` PnL $${t.pnl.toFixed(2)}` : "";
      const alerts = [
        `Closed ${t.symbol}${pnlStr}`,
        ...state.alerts,
      ].slice(0, 20);
      return { ...state, openPositions: positions, closedTrades: closed, alerts };
    }

    case "STATE_SNAPSHOT": {
      const s = action.payload as Record<string, unknown>;
      return {
        ...state,
        openPositions: (s.open_positions as Record<string, TradeRecord>) || state.openPositions,
        dailyPnl: (s.daily_pnl as number) ?? state.dailyPnl,
        dailyLossHit: (s.daily_loss_hit as boolean) ?? state.dailyLossHit,
        modelVersion: (s.model_version as string) || state.modelVersion,
      };
    }

    case "LEARNING_SUMMARY": {
      const summaries = [action.payload, ...state.learningSummaries].slice(0, 10);
      const alerts = [
        `Model retrained: v${action.payload.model_version} acc=${((action.payload.accuracy || 0) * 100).toFixed(1)}%`,
        ...state.alerts,
      ].slice(0, 20);
      return {
        ...state,
        learningSummaries: summaries,
        modelVersion: action.payload.model_version,
        alerts,
      };
    }

    case "RISK_VETO": {
      const alerts = [
        `⛔ VETO ${action.payload.symbol}: ${action.payload.reason}`,
        ...state.alerts,
      ].slice(0, 20);
      return { ...state, alerts };
    }

    case "HEARTBEAT":
      return { ...state, lastHeartbeat: action.payload.ts };

    case "ERROR": {
      const alerts = [`ERROR: ${action.payload.error}`, ...state.alerts].slice(0, 20);
      return { ...state, alerts };
    }

    default:
      return state;
  }
}

export function useTradingStore() {
  const [state, dispatch] = useReducer(reducer, initialState);

  const handleEvent = useCallback(
    (event: { topic: string; payload: Record<string, unknown> }) => {
      const topicMap: Record<string, Action["type"]> = {
        "agent.signal":            "AGENT_SIGNAL",
        "coordinator.decision":    "COORDINATOR_DECISION",
        "trade.opened":            "TRADE_OPENED",
        "trade.closed":            "TRADE_CLOSED",
        "state.snapshot":          "STATE_SNAPSHOT",
        "learning.summary":        "LEARNING_SUMMARY",
        "risk.veto":               "RISK_VETO",
        "system.heartbeat":        "HEARTBEAT",
        "system.error":            "ERROR",
      };
      const actionType = topicMap[event.topic];
      if (actionType) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        dispatch({ type: actionType, payload: event.payload as any });
      }
    },
    []
  );

  return { state, handleEvent, dispatch };
}
