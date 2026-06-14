"""Real-time trading engine (POC).

Pipeline: IBKR market data -> bar feed (5s -> 1m closed bars) -> indicator
(MACD) -> strategy (long-only crossover) -> risk gate -> executor (MKT order
on IBKR paper) -> position tracker -> journal (DB + WS broker).

Single-process, single-symbol, single-strategy. The POC's job is to prove the
plumbing. The Ross strategy is a swap-in replacement on the same Strategy ABC
once the plumbing is green.
"""
