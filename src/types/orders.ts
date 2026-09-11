export type OrderVenue = 'binance_usdm' | 'binance_spot';
export type OrderSide = 'BUY' | 'SELL';
export type OrderType = 'LIMIT_MAKER' | 'MARKET' | 'STOP_MARKET';
export type OrderStatus = 'NEW' | 'PARTIALLY_FILLED' | 'FILLED' | 'CANCELED' | 'REJECTED';

export interface DerivedOrder {
  id: string;
  clientOrderId: string;
  basketId: string;
  venue: OrderVenue;
  symbol: string;
  strategy: 'Structural Grid' | 'Exposure Recovery' | 'Trend / Breakout' | 'Funding Carry';
  side: OrderSide;
  type: OrderType;
  price: number;
  size: number;
  valueUsd: number;
  status: OrderStatus;
  filledAt?: string;
  createdAt: string;
  trace: {
    strategyIntent: string;
    opportunityScore: number;
    metaBudgetFactor: number;
    riskGovernorCheck: 'PASS' | 'WARN' | 'FAIL';
    governorRule: string;
    executionRule: string;
    feeTier: string;
    slippageBps: number;
    sourceClassification: 'EXISTING' | 'DERIVED_FRONTEND' | 'PROPOSED_BACKEND';
  };
}
