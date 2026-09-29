import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
});

export const fetchSignals = () => api.get('/signals').then(res => res.data);
export const fetchRisk = () => api.get('/risk').then(res => res.data);
export const fetchTrades = () => api.get('/trades').then(res => res.data);
export const fetchPositions = () => api.get('/positions').then(res => res.data);
export const fetchPerformance = () => api.get('/performance').then(({ data }) => ({
  winRate: data.win_rate_pct ?? 0,
  pnl: data.net_pnl ?? 0,
  totalTrades: data.trade_count ?? 0,
  breakevenWinRate: data.breakeven_win_rate,
}));
export const fetchExperiment = () => api.get('/experiment').then(res => res.data);
