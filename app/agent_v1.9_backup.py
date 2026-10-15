"""
BOMA v1.9 BACKUP — Saved before v2.0 architectural improvements.
=================================================================
This file marks the v1.9 state. The actual v1.9 code is in agent.py
as it existed when this file was created (after the syntax-error fix).

TO REVERT TO v1.9:
  1. Look at git diff or compare with this file's creation timestamp
  2. The v1.8 stable backup is in agent_v1.8_stable.py
  3. The v1.9 state = agent.py after the _format_budget_answer f-string fix

KNOWN BUGS IN v1.9 (reason for v2.0 upgrade):
  BUG-01: Numeric material IDs like "2258" not extracted → falls back to session TG11
  BUG-02: "Main component responsible for price difference" → routes to BOM explosion (WRONG)
  BUG-03: Capability questions ("what can you do?") → blocked by domain guard
  BUG-04: "How much will I save?" → shows price table, not savings amount
  BUG-05: Sequential budget ("buy X then buy Y with remaining") → ignores first purchase
  BUG-06: BOM not found → "No plants found for 00000028" (confusing message)
  BUG-07: "You can only answer DEMOCAR?" → price comparison shown (wrong)
  BUG-08: Stale session - previous material bleeds into unrelated next question

WHAT v2.0 FIXES:
  - Query-first entity extraction (current query ALWAYS wins over session)
  - Numeric material ID support (2258, TG11, pure numbers)
  - Business phrase understanding layer (responsible for = cost_variance, not BOM)
  - Capability answer engine (dynamic, not DEMOCAR-hardcoded)
  - Savings calculator (dedicated formatter for "how much will I save")
  - Sequential budget planner (multi-step: buy X → remaining → buy Y)
  - BOM validation messages (BOM XXX does not exist vs confusing messages)
  - Material name preservation (MZ-FG-M500 not truncated to MZ-FG)
"""
# This is a marker file. The actual implementation is in agent.py.
# See agent_v1.8_stable.py for the full working v1.8 codebase.
raise ImportError(
    "This is a backup marker file. Import agent.py for the current implementation."
)
