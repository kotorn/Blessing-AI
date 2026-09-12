import { evaluatePreflight } from '../src/backend/system';
import { TradingSystemState } from '../src/backend/types';

describe('System Preflight Execution Enforcements', () => {
  it('should reject TESTNET arming if synchronized to MAINNET', () => {
    const mockState: TradingSystemState = {
      dataSource: 'BINANCE',
      exchangeEnvironment: 'BINANCE_MAINNET',
      executionMode: 'PAPER',
      engineState: 'DISARMED',
      accountSynchronized: true,
      marketDataHealthy: true,
      privateStreamHealthy: true,
      tradingConnectionHealthy: true,
      reconciliationStatus: 'IN_SYNC',
      killSwitchActive: false,
      pauseNewRisk: false,
      recoveryOnly: false,
      configVersion: '1',
      updatedAt: 'now'
    };

    const requestedConfig = { executionMode: 'TESTNET', instruments: ['BTCUSDT'], strategies: { grid: true } };
    
    const result = evaluatePreflight(mockState, requestedConfig);
    
    expect(result.canArm).toBe(false);
    expect(result.checks.find(c => c.id === 'CHK-ENV-MISMATCH')).toBeDefined();
  });

  it('should reject LIVE arming globally', () => {
    const mockState: TradingSystemState = {
      dataSource: 'BINANCE',
      exchangeEnvironment: 'BINANCE_MAINNET',
      executionMode: 'PAPER',
      engineState: 'DISARMED',
      accountSynchronized: true,
      marketDataHealthy: true,
      privateStreamHealthy: true,
      tradingConnectionHealthy: true,
      reconciliationStatus: 'IN_SYNC',
      killSwitchActive: false,
      pauseNewRisk: false,
      recoveryOnly: false,
      configVersion: '1',
      updatedAt: 'now'
    };

    const requestedConfig = { executionMode: 'LIVE', instruments: ['BTCUSDT'], strategies: { grid: true } };
    
    const result = evaluatePreflight(mockState, requestedConfig);
    
    expect(result.canArm).toBe(false);
  });
});
