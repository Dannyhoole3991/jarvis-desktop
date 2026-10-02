"""
LIVE trading subsystem: the only code in this project that can ever touch real money.

It is deliberately separate from the research side:
  - research modules (lab, batch, daemon, supervisor, paper, promotion, ...) never import this package
    (enforced by tests/test_separation.py);
  - nothing here runs unless a human has (1) approved a promotion, then (2) armed live trading with a typed
    confirmation naming the strategy and capital; both are logged and reversible;
  - every entry still passes the independent live Risk Engine, which the strategy cannot change.

Disabled by default. With no ARMED file there is no code path that submits a real order.
"""
