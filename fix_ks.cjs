const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(/  auditRepository\.logEvent\(\{\n    eventType: ksActive \? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',\n    previousState: prevState,\n    newState: targetState as any,\n    executionMode: tradingSystemState\.executionMode,\n    reason: ksActive \? 'Kill switch engaged' : 'Kill switch released'\n  \}\);\n  auditRepository\.logEvent\(\{\n    eventType: ksActive \? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',\n    previousState: prevState,\n    newState: targetState as any,\n    executionMode: tradingSystemState\.executionMode,\n    reason: ksActive \? 'Kill switch engaged' : 'Kill switch released'\n  \}\);/g, 
  `  auditRepository.logEvent({
    eventType: ksActive ? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',
    previousState: prevState,
    newState: targetState as any,
    executionMode: tradingSystemState.executionMode,
    reason: ksActive ? 'Kill switch engaged' : 'Kill switch released'
  });`);

fs.writeFileSync('server.ts', code);
