import { describe, it, expect, beforeEach } from 'vitest';
import { evaluatePreflight, validateStateTransition, canExecuteAction, EXECUTION_CAPABILITIES } from '../src/backend/system';
import { TradingSystemState, EngineState } from '../src/backend/types';

describe('State Machine Transitions', () => {
  it('DISARMED -> PAPER ARMED (ARMED) is allowed', () => {
    expect(validateStateTransition('DISARMED', 'ARMED')).toBe(true);
  });
  
  it('EMERGENCY -> ARMED is NOT allowed', () => {
    expect(validateStateTransition('EMERGENCY', 'ARMED')).toBe(false);
  });
  
  it('EMERGENCY -> DISARMED is allowed (after Kill Switch release)', () => {
    expect(validateStateTransition('EMERGENCY', 'DISARMED')).toBe(true);
  });
});

describe('Action Guards', () => {
  it('PAUSED_NEW_RISK + INCREASE_RISK is blocked', () => {
    expect(canExecuteAction('PAUSED_NEW_RISK', 'INCREASE_RISK')).toBe(false);
  });
  
  it('PAUSED_NEW_RISK + CLOSE is allowed', () => {
    expect(canExecuteAction('PAUSED_NEW_RISK', 'CLOSE')).toBe(true);
  });
  
  it('RECOVERY_ONLY + INCREASE_RISK is blocked', () => {
    expect(canExecuteAction('RECOVERY_ONLY', 'INCREASE_RISK')).toBe(false);
  });
  
  it('RECOVERY_ONLY + RECOVERY is allowed', () => {
    expect(canExecuteAction('RECOVERY_ONLY', 'RECOVERY')).toBe(true);
  });
  
  it('EMERGENCY + INCREASE_RISK is blocked', () => {
    expect(canExecuteAction('EMERGENCY', 'INCREASE_RISK')).toBe(false);
  });
});

describe('Preflight Evaluator', () => {
  let mockState: TradingSystemState;

  beforeEach(() => {
    mockState = {
      dataSource: 'SIMULATED',
      exchangeEnvironment: 'NONE',
      executionMode: 'PAPER',
      engineState: 'DISARMED',
      accountSynchronized: false,
      marketDataHealthy: true,
      privateStreamHealthy: false,
      tradingConnectionHealthy: false,
      reconciliationStatus: 'UNKNOWN',
      killSwitchActive: false,
      pauseNewRisk: false,
      recoveryOnly: false,
      configVersion: 'v0.2.0',
      updatedAt: new Date().toISOString()
    };
  });

  it('PAPER with no Binance credentials passes preflight', () => {
    const requestedConfig = { executionMode: 'PAPER', instruments: ['BTCUSDT'], strategies: { grid: true } };
    const result = evaluatePreflight(mockState, requestedConfig);
    expect(result.canArm).toBe(true);
    expect(result.checks.find(c => c.id === 'CHK-SYNC')?.status).toBe('WARN');
  });

  it('TESTNET without Testnet adapter capability fails preflight', () => {
    mockState.accountSynchronized = true;
    mockState.dataSource = 'BINANCE';
    mockState.exchangeEnvironment = 'BINANCE_TESTNET';
    EXECUTION_CAPABILITIES.testnet = false;
    
    const requestedConfig = { executionMode: 'TESTNET', instruments: ['BTCUSDT'], strategies: { grid: true } };
    const result = evaluatePreflight(mockState, requestedConfig);
    expect(result.canArm).toBe(false);
    expect(result.checks.find(c => c.id === 'CHK-TESTNET-GUARD')?.status).toBe('FAIL');
  });

  it('LIVE always fails preflight', () => {
    mockState.accountSynchronized = true;
    mockState.dataSource = 'BINANCE';
    mockState.exchangeEnvironment = 'BINANCE_MAINNET';
    EXECUTION_CAPABILITIES.live = false;
    
    const requestedConfig = { executionMode: 'LIVE', instruments: ['BTCUSDT'], strategies: { grid: true } };
    const result = evaluatePreflight(mockState, requestedConfig);
    expect(result.canArm).toBe(false);
    expect(result.checks.find(c => c.id === 'CHK-LIVE-GUARD')?.status).toBe('FAIL');
  });

  it('Successful Binance Mainnet sync retains executionMode = PAPER', () => {
    // This tests the logic that sync doesn't force LIVE execution.
    // Sync updating dataSource and exchangeEnvironment is handled by /api/binance/sync-account route, 
    // but we can verify evaluatePreflight correctly understands a synced state with PAPER mode.
    mockState.accountSynchronized = true;
    mockState.dataSource = 'BINANCE';
    mockState.exchangeEnvironment = 'BINANCE_MAINNET';
    
    const requestedConfig = { executionMode: 'PAPER', instruments: ['BTCUSDT'], strategies: { grid: true } };
    const result = evaluatePreflight(mockState, requestedConfig);
    
    expect(result.canArm).toBe(true);
    expect(result.checks.find(c => c.id === 'CHK-SYNC')?.status).toBe('PASS');
    expect(result.executionMode).toBe('PAPER'); // execution mode should remain PAPER
  });
});
