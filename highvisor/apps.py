"""apps — profiles for probing whether a known app is up, and in what STATE.

A profile names the app's window (title substring) and, optionally, a localhost
port that is only open in a particular state. For Caves of Qud the Raves mod's
bridge (127.0.0.1:48710) starts lazily on the first IN-GAME tick, so:
  • no window                    -> "off"
  • window present, port closed  -> "menu"  (launched, not in a game yet)
  • window present, port open    -> "in-game"
which lets highvisor answer "is Qud up, and are we in a game yet?" with no OCR.

Caveat: the bridge keeps listening once started, so returning to the menu AFTER a
game still reads "in-game" for that session — the signal is "has a game begun",
which is exactly what a turn-driven export (or reconnect) waits on.
"""
PROFILES = {
    "qud": {
        "window": "CavesOfQud",
        "proc": "CoQ",           # pkill -f pattern for a CLEAN restart (kills duplicates too)
        "launcher": "qud_solo",
        "port": 48710,           # Raves mod bridge — opens on the first in-game tick
        "port_state": "in-game",
        "window_state": "menu",
        "off_state": "off",
    },
    "raves": {
        "window": "Raves of Qud",
        "proc": "RavesOfQud",
        # raves_USER, not raves_solo. Both are solo (neither spawns a Qud), but raves_solo passes
        # --one-to-one, which LOCKS parity mode for the run through Settings.one_to_one_only — a
        # runtime flag, so the app reports mode=1to1 while its settings.json still says "user".
        # A restart is recovery, and recovery must not silently change what the app IS. The
        # `* -> title` edge in gametree.json restarts raves, so `hv goto raves in_game` after any
        # crash used to hand back a 1:1 app: no neighbour zones, ZoneRenderer._build_darkness
        # returning immediately, and every appearance measurement taken on it quietly worthless.
        # That cost most of a session's lighting work, twice. Ask for 1:1 explicitly with
        # `hv launch raves_solo` when you want the parity pressure valve.
        "launcher": "raves_user",
    },
}

# ---------------------------------------------------------------- classification

import re

_RAVES_OWNER = re.compile(r"raves.?of.?qud|ravesofqud", re.I)
_QUD_OWNER = re.compile(r"caves ?of ?qud|cavesofqud", re.I)


def classify_target(title, class_name="", path=""):
    """``"raves"`` / ``"qud"`` / ``None`` for one window record — THE pair
    classifier, one implementation for every consumer on every OS (the cockpit's
    classifyRavesQud reads the ``role`` field this stamps onto list_targets rows;
    JS-side rules previously drifted per-OS and broke the Start flow on Windows).

    Title-only matching is never trusted alone — a browser tab or terminal
    showing "raves-of-qud" must not classify (the exception: Qud's EXACT caption
    on its Unity window class). macOS reports the owning app in class_name/path
    ("RavesOfQud", "CavesOfQud", "CoQ.app"); Windows reports the Win32 window
    class (Godot = "Engine", Unity = "UnityWndClass") with the caption in title;
    the Godot EDITOR is excluded by its "<project> - Godot Engine" title.
    Verified fixtures: tools/test_cockpit_classify.py (SPOT tier)."""
    t = (title or "").lower()
    o = (class_name or "").lower()
    p = (path or "").lower()
    if _RAVES_OWNER.search(o) or _RAVES_OWNER.search(p):
        return "raves"
    if o in ("godot", "engine") and "raves" in t and "godot engine" not in t:
        return "raves"
    if _QUD_OWNER.search(o) or "coq.app" in p or _QUD_OWNER.search(p):
        return "qud"
    if o == "unitywndclass" and t == "cavesofqud":
        return "qud"
    return None
