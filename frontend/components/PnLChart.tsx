"use client";
// components/PnLChart.tsx
import React, { useMemo } from "react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { TradeRecord } from "../lib/tradingStore";

type Props = { trades: TradeRecord[] };

export default function PnLChart({ trades }: Props) {
  const data = useMemo(() => {
    let cumulative = 0;
    return [...trades]
      .reverse()
      .filter((t) => t.pnl !== undefined)
      .map((t) => {
        cumulative += t.pnl!;
        return {
          date: t.exit_ts ? new Date(t.exit_ts).toLocaleDateString() : "",
          pnl: parseFloat(cumulative.toFixed(2)),
        };
      });
  }, [trades]);

  if (data.length === 0) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex items-center justify-center h-40">
        <p className="text-xs text-gray-600">No trade data yet for chart.</p>
      </div>
    );
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">
        Cumulative PnL
      </h2>
      <ResponsiveContainer width="100%" height={140}>
        <LineChart data={data}>
          <XAxis
            dataKey="date"
            tick={{ fill: "#6b7280", fontSize: 10 }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            tick={{ fill: "#6b7280", fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v) => `$${v}`}
          />
          <Tooltip
            contentStyle={{ background: "#111827", border: "1px solid #374151", borderRadius: 8 }}
            labelStyle={{ color: "#9ca3af", fontSize: 11 }}
            itemStyle={{ color: "#10b981", fontSize: 11 }}
            formatter={(v: number) => [`$${v.toFixed(2)}`, "PnL"]}
          />
          <ReferenceLine y={0} stroke="#374151" strokeDasharray="3 3" />
          <Line
            type="monotone"
            dataKey="pnl"
            stroke="#10b981"
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
