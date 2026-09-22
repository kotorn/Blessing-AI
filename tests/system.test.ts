import { describe, it, expect } from 'vitest';
import { canExecuteAction, evaluatePreflight, isWorkerTradingConnectionHealthy, SUPPORTED_SYMBOLS_BY_MODE } from '../src/backend/system';
import { TradingSystemState } from '../src/backend/types';
import {
  parsePortfolioMarginResponse,
  unavailablePortfolioMarginObservation,
} from '../src/backend/portfolio-margin';

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

  it('should keep LIVE fail-closed until the Mainnet release gates pass', () => {
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

    const requestedConfig = { executionMode: 'LIVE', instruments: ['ETHUSDC'], strategies: { grid: true } };
    
    const result = evaluatePreflight(mockState, requestedConfig);
    
    expect(result.canArm).toBe(false);
    expect(result.checks.find(c => c.id === 'CHK-MAINNET-APPROVAL')?.message).toContain('MAINNET_LIVE_APPROVED');
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

  it('fails closed when transport state is READY but worker health is false', () => {
    expect(isWorkerTradingConnectionHealthy({
      connection_state: 'READY',
      trading_connection_healthy: false,
    })).toBe(false);
    expect(isWorkerTradingConnectionHealthy({
      connection_state: 'READY',
      trading_connection_healthy: true,
    })).toBe(true);
    expect(isWorkerTradingConnectionHealthy({ connection_state: 'READY' })).toBe(false);
  });

  it('keeps Portfolio Margin observations read-only and separate from Worker collateral', () => {
    const observation = parsePortfolioMarginResponse([
      {
        asset: 'usdc',
        totalWalletBalance: '100',
        crossMarginAsset: '100',
        crossMarginFree: '99.5',
      },
    ]);

    expect(observation.status).toBe('OBSERVED_READ_ONLY');
    expect(observation.verified).toBe(false);
    expect(observation.includedInWorkerCollateral).toBe(false);
    expect(observation.balances[0].asset).toBe('USDC');
    expect(observation.balances[0].crossMarginAsset).toBe(100);
  });

  it('does not turn a malformed Portfolio Margin response into a zero balance', () => {
    const observation = parsePortfolioMarginResponse([
      { asset: 'USDC', totalWalletBalance: '100', crossMarginFree: '100' },
    ]);

    expect(observation.status).toBe('INVALID_RESPONSE');
    expect(observation.balances).toEqual([]);
    expect(unavailablePortfolioMarginObservation().includedInWorkerCollateral).toBe(false);
  });

  it('supports expanded instruments universe across PAPER, TESTNET, and LIVE', () => {
    expect(SUPPORTED_SYMBOLS_BY_MODE.PAPER).toContain('SOLUSDT');
    expect(SUPPORTED_SYMBOLS_BY_MODE.PAPER).toContain('BNBUSDT');
    expect(SUPPORTED_SYMBOLS_BY_MODE.PAPER).toContain('ETHUSDC');
    expect(SUPPORTED_SYMBOLS_BY_MODE.TESTNET).toContain('SOLUSDT');
    expect(SUPPORTED_SYMBOLS_BY_MODE.TESTNET).toContain('ETHUSDT');
    expect(SUPPORTED_SYMBOLS_BY_MODE.LIVE).toContain('ETHUSDC');
    expect(SUPPORTED_SYMBOLS_BY_MODE.LIVE).toContain('BTCUSDT');

    const paperState: TradingSystemState = {
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
    };

    const paperResult = evaluatePreflight(paperState, {
      executionMode: 'PAPER',
      instruments: ['BTCUSDT', 'SOLUSDT', 'BNBUSDT'],
      strategies: { grid: true },
      riskProfile: 'BALANCED',
    });

    const instCheck = paperResult.checks.find((c) => c.id === 'CHK-INSTRUMENTS');
    expect(instCheck?.status).toBe('PASS');
    expect(paperResult.canArm).toBe(true);
  });
});
