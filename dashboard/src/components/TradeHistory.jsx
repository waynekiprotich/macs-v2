import React from 'react';
import { ArrowUpRight, ArrowDownRight } from 'lucide-react';

export default function TradeHistory({ trades }) {
  if (!trades) {
    return <div className="text-gray-400 py-4 text-center">Trade history unavailable</div>;
  }
  if (trades.length === 0) {
    return <div className="text-gray-400 py-4 text-center">No trade history available</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left border-collapse">
        <thead>
          <tr className="border-b border-githubBorder text-gray-400 text-sm">
            <th className="pb-2 font-medium">Symbol</th>
            <th className="pb-2 font-medium">Type</th>
            <th className="pb-2 font-medium">Stake</th>
            <th className="pb-2 font-medium">Status</th>
            <th className="pb-2 font-medium">P&L</th>
            <th className="pb-2 font-medium">Date</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade, idx) => {
            const isProfit = trade.pnl > 0;
            const isLoss = trade.pnl < 0;
            return (
              <tr key={idx} className="border-b border-githubBorder/50 hover:bg-[#0d1117] transition-colors">
                <td className="py-3 font-semibold text-white">{trade.symbol || 'N/A'}</td>
                <td className="py-3">
                  <span className={`flex items-center text-xs font-medium ${
                    trade.side === 'BUY' ? 'text-green-400' : 'text-red-400'
                  }`}>
                    {trade.side === 'BUY' ? <ArrowUpRight size={14} className="mr-1" /> : <ArrowDownRight size={14} className="mr-1" />}
                    {trade.side || '-'}
                  </span>
                </td>
                <td className="py-3 text-gray-300">${trade.price?.toFixed(2) || '0.00'}</td>
                <td className="py-3 text-gray-300">{trade.status || 'Unknown'}</td>
                <td className={`py-3 font-medium ${
                  isProfit ? 'text-green-400' : isLoss ? 'text-red-400' : 'text-gray-400'
                }`}>
                  {isProfit ? '+' : ''}{trade.pnl == null ? 'Pending' : trade.pnl.toFixed(2)}
                </td>
                <td className="py-3 text-sm text-gray-500">
                  {trade.timestamp ? new Date(trade.timestamp).toLocaleDateString() : '-'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
