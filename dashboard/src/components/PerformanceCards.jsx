import React from 'react';
import { DollarSign, Percent, Briefcase, Activity } from 'lucide-react';

export default function PerformanceCards({ performance, positions, trades }) {
  const cards = [
    {
      title: "Total P&L",
      value: performance ? `$${performance.pnl.toLocaleString(undefined, {minimumFractionDigits: 2})}` : 'Unavailable',
      icon: <DollarSign className="text-green-500" size={24} />,
      color: performance?.pnl >= 0 ? "text-green-400" : "text-red-400"
    },
    {
      title: "Win Rate",
      value: performance ? `${performance.winRate.toFixed(1)}%` : 'Unavailable',
      icon: <Percent className="text-blue-500" size={24} />,
      color: "text-white"
    },
    {
      title: "Active Positions",
      value: positions?.length ?? 'Unavailable',
      icon: <Briefcase className="text-purple-500" size={24} />,
      color: "text-white"
    },
    {
      title: "Settled Trades",
      value: performance?.totalTrades ?? 'Unavailable',
      icon: <Activity className="text-orange-500" size={24} />,
      color: "text-white"
    }
  ];

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      {cards.map((card, idx) => (
        <div key={idx} className="bg-[#161b22] border border-githubBorder rounded-lg p-4 flex items-center justify-between">
          <div>
            <p className="text-sm text-gray-400 mb-1">{card.title}</p>
            <h3 className={`text-2xl font-bold ${card.color}`}>{card.value}</h3>
          </div>
          <div className="p-3 bg-[#0d1117] rounded-full border border-githubBorder">
            {card.icon}
          </div>
        </div>
      ))}
    </div>
  );
}
