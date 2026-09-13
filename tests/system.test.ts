import { describe, it, expect } from 'vitest';
import { canExecuteAction, evaluatePreflight } from '../src/backend/system';
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

  it('should reject TESTNET arming when the exchange environment is unknown', () => {
    const mockState: TradingSystemState = {
      dataSource: 'SIMULATED',
      exchangeEnvironment: 'NONE',
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
      updatedAt: 'now',
      workerResponsive: true,
    };

    const result = evaluatePreflight(mockState, {
      executionMode: 'TESTNET',
      instruments: ['BTCUSDT'],
      strategies: { grid: true },
      riskProfile: 'CONSERVATIVE',
    });

    expect(result.canArm).toBe(false);
    expect(result.checks.find(c => c.id === 'CHK-ENV-MISMATCH')?.status).toBe('FAIL');
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

  it('keeps UI action policy aligned with the worker risk classes', () => {
    expect(canExecuteAction('ARMED', 'NEW_RISK')).toBe(true);
    expect(canExecuteAction('PAUSED_NEW_RISK', 'NEW_RISK')).toBe(false);
    expect(canExecuteAction('PAUSED_NEW_RISK', 'REDUCE_RISK')).toBe(true);
    expect(canExecuteAction('RECOVERY_ONLY', 'INCREASE_RISK')).toBe(false);
    expect(canExecuteAction('RECOVERY_ONLY', 'RECOVERY')).toBe(true);
    expect(canExecuteAction('EMERGENCY', 'RECOVERY')).toBe(false);
    expect(canExecuteAction('EMERGENCY', 'EMERGENCY')).toBe(true);
    expect(canExecuteAction('DISARMED', 'CLOSE')).toBe(false);
  });
});
