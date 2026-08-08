#!/usr/bin/env python3
"""Drive every reachable node of one app and report what arrived — the FULL 3 whole-tree tour.

    python3 tools/tour.py raves                 # Wander shape: no reload between nodes
    python3 tools/tour.py qud --reload SAVE     # Classic shape: reload the save before EVERY node
    python3 tools/tour.py raves --only status_  # just the nodes whose id starts with this

WHY THIS IS A FILE NOW. This tour has been re-typed as ad-hoc shell in every session that ran
it, and it went wrong the same way each time: an earlier version string-matched the daemon's
output and mis-labelled an arrival as REFUSED (docs/testing.md, 2026-08-08). `parity.py capture`
was made first-class for exactly this reason. The classification below is the whole point of the
script, so it lives under version control next to the tree it exercises.

THREE OUTCOMES, and conflating them is what makes a tour unreadable:

  EDGE     the route ran and did not arrive — a real defect in the tree or the app
  ENV      the harness could not even try (bridge down, state file stale, reload failed).
           NOT an edge defect. On 2026-08-07 twelve phantom Raves failures were ENV, caused
           by driving the two apps out of sync, and reading them as EDGE cost a session.
  REFUSED  the planner declined on purpose (e.g. a modal it must not answer). Also not a
           defect — but it is not an arrival either, so it is never counted as one.

Arrival is decided by `hv assert`, i.e. by the tree's own detectors, never by the exit code of
`hv goto` and never by matching text in its output.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HV = os.path.expanduser("~/bin/hv")
STATE = {"qud": os.path.expanduser("~/Library/Application Support/RavesOfQud/qud_state.json"),
         "raves": os.path.expanduser("~/Library/Application Support/RavesOfQud/raves_state.json")}


def hv(*args, timeout=400):
    p = subprocess.run([HV] + list(args), capture_output=True, text=True, timeout=timeout)
    i = p.stdout.find("{")
    try:
        return json.loads(p.stdout[i:]) if i >= 0 else {}
    except ValueError:
        return {}


def env_ok(app):
    """Is the harness in a position to even try? Returns (ok, why).

    Sampled BEFORE each node so an environment failure cannot be misread as an edge defect.
    The 6s TTL is the heartbeat's: a state file older than that means the app stopped
    reporting, which looks exactly like a recipe that does not arrive.
    """
    path = STATE.get(app)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False, "no state file"
    if age > 6:
        return False, "state file stale (%.0fs)" % age
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=["qud", "raves"])
    ap.add_argument("--reload", metavar="SAVE",
                    help="reload this save before EVERY node (the Classic tour shape). "
                         "Uniform on purpose: without it the first restart leaves the app at "
                         "the title and later nodes stop testing what the tour is named after.")
    ap.add_argument("--only", default="", help="only nodes whose id starts with this")
    ap.add_argument("--home", default="in_game", help="node to return to between nodes")
    a = ap.parse_args()

    plan = hv("plan", a.app)
    nodes = sorted(plan.get("reachable") or {}, key=lambda n: plan["reachable"][n])
    if not nodes:                      # older daemons print a table, not json
        out = subprocess.run([HV, "plan", a.app], capture_output=True, text=True).stdout
        nodes = [l.split()[-1] for l in out.splitlines()[1:] if l.strip()]
    nodes = [n for n in nodes if n != a.home and n.startswith(a.only)]
    if not nodes:
        sys.exit("no nodes matched --only %r" % a.only)
    print("%s tour: %d nodes%s\n" % (a.app, len(nodes),
                                     ", reloading %r before each" % a.reload if a.reload else ""))

    t0 = time.time()
    res = {"arrived": [], "EDGE": [], "ENV": [], "REFUSED": []}
    for i, node in enumerate(nodes, 1):
        ok, why = env_ok(a.app)
        if not ok:
            res["ENV"].append((node, why))
            print("%2d/%d %-22s ENV  %s" % (i, len(nodes), node, why))
            continue
        if a.reload:
            r = hv("loadsave", a.reload)
            if not r.get("ok"):
                res["ENV"].append((node, "reload failed: %s" % r.get("error")))
                print("%2d/%d %-22s ENV  reload failed" % (i, len(nodes), node))
                continue
        t = time.time()
        g = hv("goto", a.app, node)
        # ARRIVAL IS DECIDED BY THE TREE, not by goto's own verdict.
        chk = hv("assert", "--app", a.app, "--node", node)
        took = time.time() - t
        # `ok` is the ENVELOPE (the op ran); `passed` is the VERDICT. Reading `ok` here made
        # every node "arrive" no matter what the assertion said -- a 22/22 that could not have
        # produced any other number. Ninth instance of that family in this codebase, so: demand
        # the field, and fail loudly rather than fall back if it is ever missing.
        if "passed" not in chk:
            sys.exit("hv assert returned no `passed` field for %s -- refusing to guess a "
                     "verdict from the envelope. Got: %s" % (node, chk))
        if chk["passed"]:
            res["arrived"].append(node)
            print("%2d/%d %-22s ok   %5.1fs%s" % (i, len(nodes), node, took,
                                                  "  (goto said no)" if not g.get("ok") else ""))
        elif g.get("refused"):
            res["REFUSED"].append((node, g.get("error", "")[:70]))
            print("%2d/%d %-22s REF  %s" % (i, len(nodes), node, g.get("error", "")[:60]))
        else:
            ok2, why2 = env_ok(a.app)
            if not ok2:
                res["ENV"].append((node, why2))
                print("%2d/%d %-22s ENV  %s (during)" % (i, len(nodes), node, why2))
            else:
                res["EDGE"].append((node, (g.get("error") or chk.get("error") or "")[:70]))
                print("%2d/%d %-22s EDGE %s" % (i, len(nodes), node,
                                                (g.get("error") or "")[:60]))
        if not a.reload:
            hv("goto", a.app, a.home)

    n = len(nodes)
    # A TOUR THAT FAILS EVERYTHING IS A BROKEN TOUR until proven otherwise. The first run of
    # this script reported 0/8 EDGE because the assert was invoked with the wrong flags and
    # could never pass -- while the gotos underneath had all arrived. Blaming the tree for a
    # clean sweep of failures is the same mistake in the other direction, so say it out loud.
    if n > 2 and not res["arrived"]:
        print("\n!! EVERY node failed. Before reading this as %d defects, check the HARNESS: "
              "run one `hv goto` and one `hv assert` by hand and confirm they agree." % n)
    print("\n%s: %d/%d arrived   EDGE %d   ENV %d   REFUSED %d   (%.1f min)"
          % (a.app, len(res["arrived"]), n, len(res["EDGE"]), len(res["ENV"]),
             len(res["REFUSED"]), (time.time() - t0) / 60))
    for kind in ("EDGE", "ENV", "REFUSED"):
        for node, why in res[kind]:
            print("  %-8s %-22s %s" % (kind, node, why))
    return 1 if res["EDGE"] else 0


if __name__ == "__main__":
    sys.exit(main())
