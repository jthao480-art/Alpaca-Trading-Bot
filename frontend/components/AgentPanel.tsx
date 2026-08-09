"use client";
// components/AgentPanel.tsx
import React from "react";
import { AgentSignal } from "../lib/tradingStore";

const AGENT_COLORS: Record<string, string> = {
  news:         "text-sky-400",
  wallet:       "text-yellow-400",
  momentum:     "text-emerald-400",
  volume:       "text-purple-400",
  forecast:     "text-pink-400",
  fundamentals: "text-orange-400",
};

const DIR_BADGE: Record<string, string> = {
  buy:  "bg-emerald-900 text-emerald-300",
  sell: "bg-red-900 text-red-300",
  hold: "bg-gray-800 text-gray-400",
};

function ScoreBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color =
    pct >= 60 ? "bg-emerald-500" : pct <= 40 ? "bg-red-500" : "bg-yellow-500";
  return (
    <div className="w-full bg-gray-800 rounded-full h-1.5 mt-1">
      <div className={`${color} h-1.5 rounded-full`} style={{ width: `${pct}%` }} />
    </div>
  );
}

type Props = { signals: AgentSignal[] };

export default function AgentPanel({ signals }: Props) {
  // Group by symbol
  const bySymbol: Record<string, AgentSignal[]> = {};
  signals.forEach((s) => {
    if (!bySymbol[s.symbol]) bySymbol[s.symbol] = [];
    bySymbol[s.symbol].push(s);
  });

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">
        Agent Signals
      </h2>
      {Object.keys(bySymbol).length === 0 && (
        <p className="text-xs text-gray-600">Waiting for signals…</p>
      )}
      {Object.entries(bySymbol).map(([symbol, sigs]) => (
        <div key={symbol} className="mb-4">
          <div className="text-xs font-bold text-white mb-2">{symbol}</div>
          <div className="space-y-1">
            {sigs.map((sig) => (
              <div key={sig.agent} className="flex items-center gap-2">
                <span
                  className={`w-24 text-xs font-semibold ${
                    AGENT_COLORS[sig.agent] || "text-gray-300"
                  }`}
                >
                  {sig.agent}
                </span>
                <span
                  className={`text-xs px-1.5 py-0.5 rounded ${
                    DIR_BADGE[sig.direction] || DIR_BADGE.hold
                  }`}
                >
                  {sig.direction}
                </span>
                <div className="flex-1">
                  <ScoreBar value={sig.score} />
                </div>
                <span className="text-xs text-gray-500 w-8 text-right">
                  {(sig.score * 100).toFixed(0)}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
