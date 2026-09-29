import React from 'react';

export default function SignalPanel({ signals }) {
  if (!signals) {
    return <div className="text-gray-400 py-4 text-center">Signals unavailable</div>;
  }
  if (signals.length === 0) {
    return <div className="text-gray-400 py-4 text-center">No active signals</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left border-collapse">
        <thead>
          <tr className="border-b border-githubBorder text-gray-400 text-sm">
            <th className="pb-2 font-medium">Symbol</th>
            <th className="pb-2 font-medium">Decision</th>
            <th className="pb-2 font-medium">Price</th>
            <th className="pb-2 font-medium" title="Indicator agreement is not a probability of winning">Agreement</th>
            <th className="pb-2 font-medium">Time</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((sig, idx) => (
            <tr key={idx} className="border-b border-githubBorder/50 hover:bg-[#0d1117] transition-colors">
              <td className="py-3 font-semibold text-white">{sig.symbol || 'N/A'}</td>
              <td className="py-3">
                <span className={`px-2 py-1 rounded text-xs font-medium ${
                  sig.action === 'BUY' ? 'bg-green-500/20 text-green-400 border border-green-500/30' : 
                  sig.action === 'SELL' ? 'bg-red-500/20 text-red-400 border border-red-500/30' : 
                  'bg-gray-500/20 text-gray-400 border border-gray-500/30'
                }`}>
                  {sig.action_taken || sig.action || 'HOLD'}
                </span>
                <p className="mt-1 text-xs text-gray-400">{sig.reasoning}</p>
              </td>
              <td className="py-3 text-gray-300">{sig.price?.toFixed(2) ?? 'Unavailable'}</td>
              <td className="py-3 text-gray-300">
                <div className="flex items-center gap-2">
                  <div className="w-16 h-2 bg-gray-700 rounded-full overflow-hidden">
                    <div 
                      className="h-full bg-blue-500" 
                      style={{ width: `${sig.agreement_score ?? 0}%` }}
                    />
                  </div>
                  <span className="text-xs">{sig.agreement_score ?? 0}%</span>
                </div>
              </td>
              <td className="py-3 text-sm text-gray-500">
                {sig.timestamp ? new Date(sig.timestamp).toLocaleTimeString() : '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
