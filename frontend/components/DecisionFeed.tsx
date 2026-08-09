"use client";
// components/DecisionFeed.tsx
import React from "react";
import { Decision } from "../lib/tradingStore";

const ACTION_STYLE: Record<string, string> = {
  buy:  "border-l-4 border-emerald-500 bg-emerald-950/40",
  sell: "border-l-4 border-red-500 bg-red-950/40",
  hold: "border-l-4 border-gray-700 bg-gray-900",
};

const ACTION_BADGE: Record<string, string> = {
  buy:  "bg-emerald-700 text-emerald-100",
  sell: "bg-red-700 text-red-100",
  hold: "bg-gray-700 text-gray-300",
};

function ScoreMeter({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color = pct >= 60 ? "#10b981" : pct <= 46 ? "#ef4444" : "#eab308";
  return (
    <div className="flex items-center gap-2">
      <div className="w-24 bg-gray-800 rounded-full h-2">
        <div
          className="h-2 rounded-full transition-all"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <span className="text-xs text-gray-400">{pct}%</span>
    </div>
  );
}

type Props = { decisions: Decision[] };

export default function DecisionFeed({ decisions }: Props) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 h-full overflow-hidden flex flex-col">
      <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">
        Coordinator Decisions
      </h2>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {decisions.length === 0 && (
          <p className="text-xs text-gray-600">No decisions yet…</p>
        )}
        {decisions.map((d, i) => (
          <div
            key={`${d.symbol}-${d.timestamp}-${i}`}
            className={`rounded-lg p-3 ${ACTION_STYLE[d.action] || ACTION_STYLE.hold}`}
          >
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center gap-2">
                <span className="font-bold text-white">{d.symbol}</span>
                <span
                  className={`text-xs px-2 py-0.5 rounded font-semibold ${
                    ACTION_BADGE[d.action] || ACTION_BADGE.hold
                  }`}
                >
                  {d.action.toUpperCase()}
                </span>
                {d.veto && (
                  <span className="text-xs bg-orange-900 text-orange-300 px-1.5 py-0.5 rounded">
                    VETO
                  </span>
                )}
              </div>
              <span className="text-xs text-gray-500">
                {new Date(d.timestamp).toLocaleTimeString()}
              </span>
            </div>
            <ScoreMeter score={d.weighted_score} />
            {d.veto && (
              <p className="text-xs text-orange-400 mt-1">{d.veto_reason}</p>
            )}
            {/* Signal breakdown */}
            <div className="mt-2 flex flex-wrap gap-1">
              {d.signals.map((s) => (
                <span
                  key={s.agent}
                  className="text-xs text-gray-400 bg-gray-800 rounded px-1.5 py-0.5"
                >
                  {s.agent}: {(s.score * 100).toFixed(0)}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
