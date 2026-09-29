import React, { useState, useEffect } from 'react';
import PerformanceCards from './PerformanceCards';
import SignalPanel from './SignalPanel';
import TradeHistory from './TradeHistory';
import WinRateGauge from './WinRateGauge';
import { fetchSignals, fetchRisk, fetchTrades, fetchPositions, fetchPerformance, fetchExperiment } from '../api';
import { Activity } from 'lucide-react';

export default function Dashboard() {
  const [data, setData] = useState({
    signals: null,
    risk: {},
    trades: null,
    positions: null,
    performance: null,
    experiment: null,
  });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadData = async () => {
      try {
        const [signals, risk, trades, positions, performance, experiment] = await Promise.all([
          fetchSignals().catch(() => null),
          fetchRisk().catch(() => ({})),
          fetchTrades().catch(() => null),
          fetchPositions().catch(() => null),
          fetchPerformance().catch(() => null),
          fetchExperiment().catch(() => null),
        ]);
        
        setData({ signals, risk, trades, positions, performance, experiment });
      } catch (error) {
        console.error("Error loading dashboard data", error);
      } finally {
        setLoading(false);
      }
    };
    
    loadData();
    const interval = setInterval(loadData, 10000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return <div className="flex items-center justify-center h-64"><Activity className="animate-spin text-blue-500 mr-2" /> Loading...</div>;
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-3 bg-githubDarker border border-githubBorder rounded-lg p-4">
        <p className="font-semibold">Demo experiment: {data.experiment?.experiment_id ?? 'Configuration unavailable'}</p>
        <p className="text-sm text-gray-400 mt-1">
          {data.experiment ? (data.experiment.execution_enabled && data.experiment.mode === 'PAPER'
            ? 'Demo execution enabled' : 'Observation only') : 'Execution status unavailable'}
          {' · '}Profitability has not been validated. Agreement scores are not win probabilities.
          {' '}Performance below includes all recorded history, including earlier configurations.
        </p>
        {data.experiment && <p className="text-sm text-gray-400 mt-1">
          Stake: {(data.experiment.risk_per_trade * 100).toFixed(2)}% of demo cash, capped at {data.experiment.max_stake} USD.
          {' '}Maximum open contracts: {data.experiment.max_open_trades}.
        </p>}
      </div>
      <div className="lg:col-span-3">
        <PerformanceCards performance={data.performance} positions={data.positions} trades={data.trades} />
      </div>
      
      <div className="lg:col-span-2 flex flex-col gap-6">
        <div className="bg-[#161b22] border border-githubBorder rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-4 text-white">Recent Signals</h2>
          <SignalPanel signals={data.signals} />
        </div>
        
        <div className="bg-[#161b22] border border-githubBorder rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-4 text-white">Trade History</h2>
          <TradeHistory trades={data.trades} />
        </div>
      </div>
      
      <div className="flex flex-col gap-6">
        <div className="bg-[#161b22] border border-githubBorder rounded-lg p-4 flex flex-col items-center">
          <h2 className="text-lg font-semibold mb-4 text-white self-start">Win Rate</h2>
          {data.performance ? <WinRateGauge winRate={data.performance.winRate} />
            : <p className="text-gray-400">Performance unavailable</p>}
        </div>
      </div>
    </div>
  );
}
