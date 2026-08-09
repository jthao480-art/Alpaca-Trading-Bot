"use client";
// components/AlertBanner.tsx
import React from "react";

type Props = { alerts: string[] };

export default function AlertBanner({ alerts }: Props) {
  return (
    <div className="bg-amber-950/60 border-b border-amber-700/50 px-4 py-2 overflow-hidden">
      <div className="flex gap-6 overflow-x-auto text-xs text-amber-300 whitespace-nowrap">
        {alerts.map((a, i) => (
          <span key={i} className="flex-shrink-0">
            {a}
          </span>
        ))}
      </div>
    </div>
  );
}
