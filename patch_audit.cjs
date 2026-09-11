const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(
  /import \{ evaluatePreflight, validateStateTransition, RISK_PROFILES, canExecuteAction, EXECUTION_CAPABILITIES \} from '\.\/src\/backend\/system\.js';/,
  `import { evaluatePreflight, validateStateTransition, RISK_PROFILES, canExecuteAction, EXECUTION_CAPABILITIES } from './src/backend/system.js';
import { auditRepository } from './src/backend/audit.js';`
);

const armRegex = /tradingSystemState\.executionMode = requestedConfig\.executionMode as any;\n  tradingSystemState\.engineState = 'ARMED';/;
code = code.replace(armRegex, `const prevState = tradingSystemState.engineState;
  tradingSystemState.executionMode = requestedConfig.executionMode as any;
  tradingSystemState.engineState = 'ARMED';
  auditRepository.logEvent({
    eventType: 'ENGINE_ARMED',
    previousState: prevState,
    newState: 'ARMED',
    executionMode: tradingSystemState.executionMode,
    reason: 'ARM requested by user and preflight passed',
    metadata: { requestedConfig }
  });`);

const disarmRegex = /tradingSystemState\.engineState = 'DISARMED';\n  tradingSystemState\.killSwitchActive = false;/;
code = code.replace(disarmRegex, `const prevState = tradingSystemState.engineState;
  tradingSystemState.engineState = 'DISARMED';
  tradingSystemState.killSwitchActive = false;
  auditRepository.logEvent({
    eventType: 'ENGINE_DISARMED',
    previousState: prevState,
    newState: 'DISARMED',
    executionMode: tradingSystemState.executionMode,
    reason: 'Manual DISARM requested'
  });`);

const ksRegex = /tradingSystemState\.killSwitchActive = ksActive;\n  tradingSystemState\.engineState = targetState as any;/;
code = code.replace(ksRegex, `const prevState = tradingSystemState.engineState;
  tradingSystemState.killSwitchActive = ksActive;
  tradingSystemState.engineState = targetState as any;
  auditRepository.logEvent({
    eventType: ksActive ? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',
    previousState: prevState,
    newState: targetState as any,
    executionMode: tradingSystemState.executionMode,
    reason: ksActive ? 'Kill switch engaged' : 'Kill switch released'
  });`);

const ks2Regex = /tradingSystemState\.killSwitchActive = ksActive;\n  tradingSystemState\.engineState = targetState as any;/g;
code = code.replace(ks2Regex, `const prevState = tradingSystemState.engineState;
  tradingSystemState.killSwitchActive = ksActive;
  tradingSystemState.engineState = targetState as any;
  auditRepository.logEvent({
    eventType: ksActive ? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',
    previousState: prevState,
    newState: targetState as any,
    executionMode: tradingSystemState.executionMode,
    reason: ksActive ? 'Kill switch engaged' : 'Kill switch released'
  });`);
// Let's not use global regex blindly if we already matched the first.
// Wait, I already matched once, but I should make sure I replace everywhere it happens. Let's just run it and see.

fs.writeFileSync('server.ts', code);
