#!/usr/bin/env python3
"""SPOT test: the Raves/Qud window classifier (highvisor.apps.classify_target).

The classifier is the cockpit's eyes — the Start buttons wait on it, the arrange
steps trust it, and it broke once already when the JS rules drifted per-OS (the
Windows Unity window class "UnityWndClass" matched nothing, so "Start Raves +
Qud" waited for a Qud that was already up and never arranged the pair). The
defect class is decidable from source, so per docs/testing.md (raves) it gets a
static fixture table: real window records from BOTH OSes, plus the known
false-positive traps (a browser tab or terminal titled like the repo, and the
Godot editor).

Run from the repo root (stdlib only, no daemon, no apps):

    python tools/test_cockpit_classify.py

Exit 0 clean; exit 1 prints each failing fixture.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from highvisor.apps import classify_target  # noqa: E402

# (title, class_name, path) -> expected role
FIXTURES = [
    # --- Windows (Lumpy), live-captured 2026-08-06 ---
    (("Raves of Qud (DEBUG)", "Engine", ""), "raves"),          # Godot dev-run
    (("CavesOfQud", "UnityWndClass", ""), "qud"),               # Unity game window
    (("Raves of Qud", "Engine", ""), "raves"),                  # exported build (Godot class)
    # --- macOS (the Mac stage; class_name = owning app / bundle) ---
    (("Raves of Qud", "RavesOfQud", ""), "raves"),
    (("CavesOfQud", "CavesOfQud", ""), "qud"),
    (("", "CavesOfQud",
      "/Users/x/Library/.../Caves of Qud/CoQ.app"), "qud"),
    (("raves — project", "Godot",
      "/Users/x/Downloads/Godot.app"), "raves"),                # mac dev-run window
    # --- traps that must NOT classify ---
    (("raves-of-qud - Godot Engine", "Godot", ""), None),       # the Godot EDITOR
    (("raves-of-qud - Godot Engine", "Engine", ""), None),      # editor, Windows class
    (("TheMutantFactory/raves-of-qud: a Godot viewer - Chrome",
      "Chrome_WidgetWin_1", ""), None),                         # browser tab
    (("MINGW64:/c/Users/danie/personal-git/raves-of-qud", "mintty", ""), None),
    (("Caves of Qud Wiki - Chrome", "Chrome_WidgetWin_1", ""), None),
    (("CavesOfQud", "Chrome_WidgetWin_1", ""), None),           # caption alone isn't enough
    (("Steam", "SDL_app", ""), None),
    (("", "", ""), None),
]


def main() -> int:
    bad = []
    for (title, cls, path), want in FIXTURES:
        got = classify_target(title, cls, path)
        if got != want:
            bad.append("  %r / class=%r -> got %r, want %r" % (title, cls, got, want))
    print("classify fixtures: %d checked, %d failed" % (len(FIXTURES), len(bad)))
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
