"""Topic strings for the in-process broker."""

CANDIDATE_UPDATE = "candidate.update"  # new or refreshed candidate
SCANNER_EVENT = "scanner.event"        # raw alert ingested
RULE_SET_CHANGED = "rules.changed"     # user edited rule set

# Engine (POC trading engine)
ENGINE_BAR = "engine.bar"                          # each closed 1m bar
ENGINE_INDICATOR = "engine.indicator"              # each MACD update
ENGINE_SIGNAL = "engine.signal"                    # each signal emitted
ENGINE_APPROVAL_NEEDED = "engine.approval_needed"  # non-autonomous run awaiting approval
ENGINE_POSITION = "engine.position"                # position state change
ENGINE_FILL = "engine.fill"                        # fill received (incl. slippage)
ENGINE_PNL = "engine.pnl"                          # rolling P&L update
ENGINE_ERROR = "engine.error"                      # errors and risk blocks
ENGINE_RUN_STATE = "engine.run_state"              # engine_run lifecycle changes
# v1.1 (FirstPullback + L2/T&S)
ENGINE_DEPTH = "engine.depth"                      # L2 book changes
ENGINE_TAPE = "engine.tape"                        # T&S prints
ENGINE_FEATURES = "engine.features"                # derived feature snapshot
