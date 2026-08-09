"use client";
// pages/index.tsx  (works as app/page.tsx too)
import React, { useEffect } from "react";
import { useEventStream } from "../hooks/useEventStream";
import { useTradingStore } from "../lib/tradingStore";
import AgentPanel from "../components/AgentPanel";
import DecisionFeed from "../components/DecisionFeed";
import PositionsTable from "../components/PositionsTable";
import TradeHistory from "../components/TradeHistory";
import AlertBanner from "../components/AlertBanner";
import StatusBar from "../components/StatusBar";
import PnLChart from "../components/PnLChart";

const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8765";

export default function Dashboard() {
  const { connected, on } = useEventStream(WS_URL);
  const { state, handleEvent, dispatch } = useTradingStore();

  useEffect(() => {
    dispatch({ type: "SET_CONNECTED", payload: connected });
  }, [connected, dispatch]);

  useEffect(() => {
    const off = on("*", handleEvent);
    return off;
  }, [on, handleEvent]);

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 font-mono">
      {/* Header */}
      <header className="border-b border-gray-800 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-2xl font-bold text-emerald-400">⚡ TradingBot v3</span>
          <StatusBar
            connected={state.connected}
            modelVersion={state.modelVersion}
            dailyPnl={state.dailyPnl}
            dailyLossHit={state.dailyLossHit}
            lastHeartbeat={state.lastHeartbeat}
          />
        </div>
        <div className="text-xs text-gray-500">
          {new Date().toLocaleTimeString()}
        </div>
      </header>

      {/* Alert banner */}
      {state.alerts.length > 0 && (
        <AlertBanner alerts={state.alerts.slice(0, 5)} />
      )}

      {/* Main grid */}
      <main className="p-4 grid grid-cols-1 xl:grid-cols-3 gap-4">
        {/* Left: Agent panel + PnL chart */}
        <div className="xl:col-span-1 flex flex-col gap-4">
          <AgentPanel signals={state.signals} />
          <PnLChart trades={state.closedTrades} />
        </div>

        {/* Centre: Decision feed */}
        <div className="xl:col-span-1">
          <DecisionFeed decisions={state.decisions} />
        </div>

        {/* Right: Positions + history */}
        <div className="xl:col-span-1 flex flex-col gap-4">
          <PositionsTable positions={state.openPositions} />
          <TradeHistory trades={state.closedTrades} />
        </div>
      </main>
    </div>
  );
}
