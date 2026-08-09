"use client";
// components/StatusBar.tsx
import React from "react";

type Props = {
  connected: boolean;
  modelVersion: string;
  dailyPnl: number;
  dailyLossHit: boolean;
  lastHeartbeat: string | null;
};

export default function StatusBar({
  connected,
  modelVersion,
  dailyPnl,
  dailyLossHit,
  lastHeartbeat,
}: Props) {
  return (
    <div className="flex items-center gap-4 text-xs">
      {/* Connection */}
      <span className={`flex items-center gap-1 ${connected ? "text-emerald-400" : "text-red-400"}`}>
        <span className={`inline-block w-2 h-2 rounded-full ${connected ? "bg-emerald-400" : "bg-red-400"}`} />
        {connected ? "Live" : "Disconnected"}
      </span>

      {/* Daily PnL */}
      <span
        className={`px-2 py-0.5 rounded ${
          dailyLossHit
            ? "bg-red-900 text-red-300"
            : dailyPnl >= 0
            ? "text-emerald-400"
            : "text-red-400"
        }`}
      >
        Day PnL: {dailyPnl >= 0 ? "+" : ""}${dailyPnl.toFixed(2)}
        {dailyLossHit && " ⛔ LIMIT"}
      </span>

      {/* Model */}
      <span className="text-gray-500">
        Model: <span className="text-gray-300">{modelVersion}</span>
      </span>

      {/* Heartbeat */}
      {lastHeartbeat && (
        <span className="text-gray-600 hidden md:inline">
          ♥ {new Date(lastHeartbeat).toLocaleTimeString()}
        </span>
      )}
    </div>
  );
}
