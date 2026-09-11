import os

with open('apps/trading_worker/main.py', 'r') as f:
    code = f.read()

code = code.replace(
    "from apps.trading_worker.engines.grid_strategy import GridStrategyEngine",
    "from apps.trading_worker.engines.grid_strategy import GridStrategyEngine\nfrom apps.trading_worker.engines.trend_strategy import TrendStrategyEngine\nfrom apps.trading_worker.engines.shock_strategy import ShockStrategyEngine"
)

code = code.replace(
    "self.grid_engine = GridStrategyEngine()",
    "self.grid_engine = GridStrategyEngine()\n        self.trend_engine = TrendStrategyEngine()\n        self.shock_engine = ShockStrategyEngine()"
)

code = code.replace(
    "grid_intent = self.grid_engine.evaluate(pa_state, market_state)",
    """grid_intent = self.grid_engine.evaluate(pa_state, market_state)
        trend_intent = self.trend_engine.evaluate(pa_state, market_state)
        shock_intent = self.shock_engine.evaluate(pa_state, market_state)
        
        intents = [i for i in [grid_intent, trend_intent, shock_intent] if i]"""
)

code = code.replace(
    "if grid_intent:",
    "for intent in intents:"
)

code = code.replace(
    "grid_intent.strategy_id",
    "intent.strategy_id"
)
code = code.replace(
    "grid_intent.opportunity_score",
    "intent.opportunity_score"
)
code = code.replace(
    "grid_intent.desired_delta_qty",
    "intent.desired_delta_qty"
)

with open('apps/trading_worker/main.py', 'w') as f:
    f.write(code)
