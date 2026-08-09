"use client";
// components/TradeHistory.tsx
import React from "react";
import { TradeRecord } from "../lib/tradingStore";

type Props = { trades: TradeRecord[] };

export default function TradeHistory({ trades }: Props) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">
        Trade History ({trades.length})
      </h2>
      <div className="overflow-y-auto max-h-64 space-y-1">
        {trades.length === 0 && (
          <p className="text-xs text-gray-600">No closed trades yet.</p>
        )}
        {trades.map((t, i) => (
          <div
            key={`${t.symbol}-${t.exit_ts}-${i}`}
            className="flex items-center justify-between text-xs py-1.5 border-b border-gray-800/50"
          >
            <span className="font-bold text-gray-200 w-16">{t.symbol}</span>
            <span className="text-gray-500 w-20">
              {t.exit_ts ? new Date(t.exit_ts).toLocaleDateString() : "—"}
            </span>
            <span className="text-gray-400 w-16 text-right">
              ${t.entry_price?.toFixed(2)} → ${t.exit_price?.toFixed(2) || "—"}
            </span>
            <span
              className={`w-20 text-right font-semibold ${
                (t.pnl || 0) >= 0 ? "text-emerald-400" : "text-red-400"
              }`}
            >
              {t.pnl !== undefined
                ? `${t.pnl >= 0 ? "+" : ""}$${t.pnl.toFixed(2)}`
                : "—"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
