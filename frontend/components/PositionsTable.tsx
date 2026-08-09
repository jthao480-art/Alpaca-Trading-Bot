"use client";
// components/PositionsTable.tsx
import React from "react";
import { TradeRecord } from "../lib/tradingStore";

type Props = { positions: Record<string, TradeRecord> };

export default function PositionsTable({ positions }: Props) {
  const rows = Object.values(positions);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">
        Open Positions ({rows.length})
      </h2>
      {rows.length === 0 ? (
        <p className="text-xs text-gray-600">No open positions.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-gray-800">
              <th className="text-left pb-1">Symbol</th>
              <th className="text-right pb-1">Qty</th>
              <th className="text-right pb-1">Entry</th>
              <th className="text-right pb-1">TP</th>
              <th className="text-right pb-1">SL</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.symbol} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="py-1.5 font-bold text-emerald-400">{t.symbol}</td>
                <td className="text-right text-gray-300">{t.qty}</td>
                <td className="text-right text-gray-300">${t.entry_price.toFixed(2)}</td>
                <td className="text-right text-emerald-400">${t.take_profit.toFixed(2)}</td>
                <td className="text-right text-red-400">${t.stop_loss.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
