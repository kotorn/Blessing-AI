export type ShockHorizon = '5s' | '15s' | '1m' | '5m';

export type ShockState = 'NORMAL' | 'ELEVATED' | 'SHOCK_EXPANSION' | 'LIQUIDITY_SWEEP';

export interface HorizonShockData {
  horizon: ShockHorizon;
  velocityPctPerSec: number;
  accelerationPctPerSec2: number;
  rollingPercentile: number; // e.g. 96.5% of moves in historical window
  zScore: number;            // standard deviations from mean velocity
  displacementBps: number;
  isShock: boolean;
}

export interface StructuralLevel {
  label: string;
  price: number;
  type: 'SWING_HIGH' | 'SWING_LOW' | 'PIVOT' | 'LIQUIDITY_SWEEP' | 'RANGE_EXPANSION';
  distancePct: number;
  reclaimStatus: 'ACCEPTED' | 'REJECTED' | 'TESTING' | 'SWEPT';
  touchCount: number;
}

export interface MarketStructureState {
  symbol: string;
  spotPrice: number;
  perpPrice: number;
  swingHigh24h: number;
  swingLow24h: number;
  primaryPivot: number;
  structuralLevels: StructuralLevel[];
  rangeExpansionRatio: number; // current range / ATR(24h)
  displacementBps: number;
  displacementDirection: 'EXPANDING_UP' | 'EXPANDING_DOWN' | 'COMPRESSING';
  marketStructureTrend: 'BULLISH_STRUCTURE' | 'BEARISH_STRUCTURE' | 'RANGE_BOUND';
}

export interface BasisCarryAnalysis {
  symbol: string;
  spotPrice: number;
  perpPrice: number;
  basisUsd: number;
  basisZScore: number;
  fundingRate8h: number;
  fundingAnnualizedPct: number;
  nextFundingCountdown: string;
  estimatedRoundTripFeePct: number;
  netExpectedCarryPct: number;
  carryViable: boolean;
}
