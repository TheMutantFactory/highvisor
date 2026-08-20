#!/usr/bin/env python3
"""selftest_steps — per-step `requires` and the `report` assert condition.

Stdlib only; no daemon, no apps. Run it with any change to step gating or assert conditions.

Both features exist because of one bug class this repo keeps meeting: a guard that reads as a
guard and is not one. Step-level `requires` was accepted by the tree and IGNORED by the engine
(only plan.py honoured it, and only for whole edges), so a step written with one ran
unconditionally while looking guarded to anyone maintaining gametree.json. That is worse than the
`os` case selftest_plan.py covers -- it does the thing rather than nothing.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from highvisor.engine import Engine
from highvisor.plan import _requires_hold

fails = []
def check(label, got, want):
    if got != want:
        fails.append("%s: got %r, wanted %r" % (label, got, want))
        print("  FAIL %s: got %r, wanted %r" % (label, got, want))
    else:
        print("  ok   %s" % label)

print("== step `requires` is STRICTER than the planner's ==")
check("met -> may run", Engine._step_requires_unmet({"game_live": True}, {"game_live": True}), {})
check("contradicted -> skip",
      Engine._step_requires_unmet({"game_live": False}, {"game_live": True}), {"game_live": True})
check("UNKNOWN -> skip (the planner would allow it)",
      Engine._step_requires_unmet({"game_live": True}, {"game_live": None}), {"game_live": None})
check("...and the planner really does allow it",
      _requires_hold({"game_live": True}, {"game_live": None}), True)
check("missing key counts as unknown",
      Engine._step_requires_unmet({"game_live": True}, {}), {"game_live": None})

print("== signals merge across apps (game_live is Qud's probe, not Raves') ==")
states = {"raves": {"signals": {"present": True, "game_live": None}},
          "qud": {"signals": {"present": True, "game_live": True}}}
check("raves step sees qud's game_live",
      Engine._merge_signals(states, "raves").get("game_live"), True)
check("the step's OWN app wins where both answer",
      Engine._merge_signals({"raves": {"signals": {"present": False}},
                             "qud": {"signals": {"present": True}}}, "raves").get("present"), False)

print("== assert `report` condition ==")
st = {"extra": {"game_live": True, "mode": "user", "snap_ts": 0}}
h = lambda want: Engine._assert_holds(None, want, st)
check("true = present and truthy", h({"report": {"game_live": True}}), True)
check("0 is NOT truthy", h({"report": {"snap_ts": True}}), False)
check("false = absent or falsy", h({"report": {"snap_ts": False}}), True)
check("absent key satisfies false", h({"report": {"nope": False}}), True)
check("string compare", h({"report": {"mode": "user"}}), True)
check("string compare, wrong", h({"report": {"mode": "1to1"}}), False)
check("all keys must hold", h({"report": {"mode": "user", "snap_ts": True}}), False)

print()
if fails:
    print("%d check(s) failed" % len(fails)); sys.exit(1)
print("all good (0 checks failed)")
