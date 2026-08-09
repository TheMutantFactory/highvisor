#!/usr/bin/env python3
"""SPOT test for gametree.evaluate — which node wins, and why. Stdlib only, nothing running.

    python3 tools/selftest_evaluate.py

THE BUG IT WAS WRITTEN FOR (observed live 2026-08-06). `hv state` reported

    qud     Title Screen  scene=play  via=live

while Qud was plainly in-game — confirmed by screenshot, which is the standing rule here.
Both `title` and `in_game` sit at depth 1 in the real tree. `in_game` matched the mod's
first-party `scene: "play"`; `title` matched its `{"game_live": false}` fallback, because the
game_live probe is a 0.35s read on Qud's bridge and a busy or just-restarted Qud can miss it.
Two matches at equal depth, and the winner was decided by which node appears FIRST in the
children array.

Harmless while a human read the line. Not harmless once `gamego` PLANS from the detected
state: a stray "title" makes it plan title->in_game, whose edge is `load_save` — reloading the
save over a running game. The refactor made a long-standing wobble consequential, which is
exactly the kind of thing that deserves a test rather than a comment.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from highvisor import gametree  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))
        FAILED.append(name)


def sig(**kw):
    s = {"present": True, "port_open": None, "game_live": None, "ocr_text": None,
         "scene": None, "report": "fresh", "tab": None}
    s.update(kw)
    return s


# Same shape as the real tree's collision: two depth-1 siblings, the shallow-signal one first.
TOY = {
    "apps": {"a": {}},
    "root": {"id": "root", "children": [
        {"id": "title", "detect": {"a": [{"scene": "MainMenu"}, {"game_live": False}]}},
        {"id": "in_game", "detect": {"a": [{"scene": "play"}, {"game_live": True}]},
         "children": [
             {"id": "status", "detect": {"a": [{"scene": "StatusScreens"}]}, "children": [
                 {"id": "status_journal",
                  "detect": {"a": [{"scene": "StatusScreens", "tab": "Journal"}]}},
             ]},
         ]},
    ]},
}


def main():
    print("evaluate: signal trust vs tree order")

    # THE REGRESSION. Both match at depth 1; the first-party scene must win over the inference.
    r = gametree.evaluate(TOY, "a", sig(scene="play", game_live=False))
    check("a first-party scene beats a stale game_live at equal depth",
          r["node"] == "in_game" and r["via"] == "scene",
          "%s via %s" % (r["node"], r["via"]))

    # ...and the reverse ordering must not have been "fixed" by simply preferring the LAST node.
    r = gametree.evaluate(TOY, "a", sig(scene="MainMenu", game_live=True))
    check("the same rule picks title when the scene says MainMenu",
          r["node"] == "title" and r["via"] == "scene", "%s via %s" % (r["node"], r["via"]))

    # Depth still dominates trust — a deeper node with a WEAKER signal should still win, or the
    # ranking would flip the tab/scene hierarchy the status screens depend on.
    r = gametree.evaluate(TOY, "a", sig(scene="StatusScreens", tab="Journal"))
    check("depth beats trust (the deepest match still wins)",
          r["node"] == "status_journal" and r["via"] == "tab",
          "%s via %s" % (r["node"], r["via"]))

    # With no first-party report at all, the inference is still allowed to decide.
    r = gametree.evaluate(TOY, "a", sig(game_live=False))
    check("game_live alone still resolves the title", r["node"] == "title" and r["via"] == "live",
          "%s via %s" % (r["node"], r["via"]))
    r = gametree.evaluate(TOY, "a", sig(game_live=True))
    check("game_live alone still resolves in-game",
          r["node"] == "in_game" and r["via"] == "live", "%s via %s" % (r["node"], r["via"]))

    # Honest unknowns, both directions.
    r = gametree.evaluate(TOY, "a", sig())
    check("nothing matched -> running, screen unknown",
          r["node"] is None and r["running"] and not r["off"], str(r))
    r = gametree.evaluate(TOY, "a", sig(present=False))
    check("no window -> off", r["off"] and not r["running"], str(r))

    check("the trust table ranks first-party above inference",
          gametree.TRUST["tab"] > gametree.TRUST["scene"] > gametree.TRUST["ocr"]
          > gametree.TRUST["live"] >= gametree.TRUST["port"])

    # The real tree, against the exact signals that produced the bad reading.
    real = gametree.load_tree(force=True)
    r = gametree.evaluate(real, "qud", sig(scene="play", game_live=False, port_open=True))
    check("REAL tree: scene=play + game_live False reads as in_game, not title",
          r["node"] == "in_game", "%s via %s" % (r["node"], r["via"]))
    # TitleScreen, not MainMenu: the mod names the title POSITIVELY now, because the legacy view
    # field says "MainMenu" for every modern WindowBase menu too, so that scene could not tell the
    # title from a chargen screen. This fixture tracked the old name and had been failing since.
    r = gametree.evaluate(real, "qud", sig(scene="TitleScreen", game_live=False, port_open=True))
    check("REAL tree: the title still reads as the title", r["node"] == "title",
          "%s via %s" % (r["node"], r["via"]))
    r = gametree.evaluate(real, "raves", sig(scene="status_journal"))
    check("REAL tree: raves status tab resolves to its leaf", r["node"] == "status_journal",
          "%s via %s" % (r["node"], r["via"]))

    # A TORN READ must not take the daemon down. The tree hot-reloads on mtime, so any
    # non-atomic writer (an editor, a script that dumps then appends) leaves a window where the
    # file is half a document — and every op goes through the tree, so a raise there answered
    # JSONDecodeError to everything until someone touched the file again.
    import os, shutil, tempfile, time
    good = gametree.load_tree(force=True)
    n = len(good.get("transitions") or [])
    path = gametree._PATH
    backup = tempfile.mktemp(suffix=".json")
    shutil.copy(path, backup)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"root": {"id": "root"')      # a half-written save
        os.utime(path, None)
        time.sleep(0.01)
        served = gametree.load_tree()
        check("a torn read keeps serving the last good tree",
              len(served.get("transitions") or []) == n)
    finally:
        shutil.copy(backup, path)
        os.remove(backup)
        gametree.load_tree(force=True)

    assert_tolerance()

    print("\n%s (%d checks failed)" % ("all good" if not FAILED else "FAILED", len(FAILED)))
    return 1 if FAILED else 0


def assert_tolerance():
    """`assert node=X` tolerates landing DEEPER than X, and must not tolerate not moving.

    THE BUG (measured live 2026-08-07). A `me_menu_file -> map_editor` edge — steps
    `[{key: escape}]`, verify `{node: map_editor}` — passed while the File dropdown was
    still open and the state file still read `tab='File'`, because map_editor is on
    me_menu_file's path. The route continued, the next click landed on the menu bar with a
    dropdown down (which cancels it and opens nothing), and `hv goto` returned ok=True
    having arrived nowhere.

    The tolerance itself is RIGHT and must survive: detection reports the deepest match, so
    an edge aiming at a container legitimately lands on a child (raves title->new_game
    arrives on game_mode). What was missing is that the tolerance is DIRECTIONAL — it may
    not swallow "I never left". _drive_route supplies `not_within` for any climbing edge;
    `exact` is the manual form.
    """
    from highvisor.engine import Engine
    holds = Engine._assert_holds        # pure over (want, state); no backend, no daemon

    IN_MENU = {"node": "me_menu_file", "path": ["title", "modding_toolkit", "map_editor",
                                                "me_menu_file"]}
    IN_EDITOR = {"node": "map_editor", "path": ["title", "modding_toolkit", "map_editor"]}
    IN_CHARGEN = {"node": "game_mode", "path": ["title", "new_game", "game_mode"]}

    print("\nassert tolerance (directional)")
    check("landing DEEPER than the asked-for node still passes",
          holds(None, {"node": "new_game"}, IN_CHARGEN))
    check("the exact node passes",
          holds(None, {"node": "map_editor"}, IN_EDITOR))

    check("an ancestor alone still passes without a direction",
          holds(None, {"node": "map_editor"}, IN_MENU))
    check("`exact` REJECTS the still-open dropdown",
          not holds(None, {"node": "map_editor", "exact": True}, IN_MENU))
    check("`exact` accepts the editor itself",
          holds(None, {"node": "map_editor", "exact": True}, IN_EDITOR))

    # what _drive_route now attaches to a climbing edge
    climb = {"node": "map_editor", "not_within": "me_menu_file"}
    check("a CLIMBING edge's verify fails while we are still where we started",
          not holds(None, climb, IN_MENU))
    check("the same verify passes once the dropdown is gone",
          holds(None, climb, IN_EDITOR))
    check("not_within rejects DESCENDANTS of the named node too",
          not holds(None, {"node": "title", "not_within": "map_editor"}, IN_MENU))
    check("not_within leaves an unrelated branch alone",
          holds(None, {"node": "new_game", "not_within": "map_editor"}, IN_CHARGEN))

    # `exact` must not be mistaken for a condition — asserting it ALONE is not an assertion
    eng = Engine.__new__(Engine)
    r = Engine._assert_state(eng, None, {"app": "qud", "exact": True})
    check("`exact` alone is not a condition", r.get("ok") is False, str(r))

    popup_conditions()
    stranded_stage()
    dead_reporter()
    mod_config_popup()


def stranded_stage():
    """A game that ENDED with the view stuck on the stage must not read as In-Game.

    THE BUG (recorded, 2026-08-07). `in_game` listed `Stage` among its scenes. The mod folds
    liveness into that field -- StartupHook's heartbeat maps `live && view in (Stage, "",
    MainMenu)` to `"play"` -- so a LIVE game always reports `play`, and the bare `Stage` can
    only mean the game is gone while the view never left. Across 913 recorded observations:
    `play` was live 251/251, `Stage` was not-live 49/49, never once the other way.

    Calling that In-Game did the damage that mattered. Of 30 recorded runs that STARTED here,
    all 10 `goto qud in_game` calls planned ZERO steps and returned ok -- so every "retry on a
    fresh load" re-tested the same dead game, which is exactly what disguised the Classic-save
    ABANDON bug as progressive process ageing for most of a session. The other 20 failed and
    left the state as they found it: the state is absorbing, and its only exit is a restart.

    Fully decidable from the signals, so it belongs here and not in a live run -- which is
    just as well, because the strand itself is intermittent and cannot be produced on demand.
    """
    real = gametree.load_tree()

    print("\nstranded stage (game over, view stuck)")

    # the exact signals recorded in goto-trace.jsonl / age_qud_quit.jsonl for the failing rows
    stranded = sig(scene="Stage", game_live=False)
    r = gametree.evaluate(real, "qud", stranded)
    check("a stranded stage is NOT In-Game", r["node"] != "in_game",
          "read as %s via %s" % (r["node"], r["via"]))
    check("a stranded stage reads as `stranded_stage`", r["node"] == "stranded_stage",
          "%s via %s" % (r["node"], r["via"]))

    # The tolerance trap: `assert node=in_game` accepts landing DEEPER, so a stranded stage
    # parked under in_game would satisfy the very check meant to catch "the game never started".
    check("`stranded_stage` is not on in_game's path",
          "in_game" not in (r.get("path") or []), str(r.get("path")))

    # and the live game must still be detected exactly as before
    r = gametree.evaluate(real, "qud", sig(scene="play", game_live=True))
    check("a live game still reads In-Game", r["node"] == "in_game" and r["via"] == "scene",
          "%s via %s" % (r["node"], r["via"]))
    for s in ("PopupMessage", "PopupText", "PopupYesNo"):
        r = gametree.evaluate(real, "qud", sig(scene=s, game_live=True))
        check("an in-game modal (%s) still reads In-Game" % s, r["node"] == "in_game",
              "%s via %s" % (r["node"], r["via"]))

    # the probe alone, with no first-party report, must still resolve in-game -- the stranded
    # node must not have stolen the game_live fallback
    r = gametree.evaluate(real, "qud", sig(game_live=True))
    check("game_live alone still resolves In-Game for qud",
          r["node"] == "in_game" and r["via"] == "live", "%s via %s" % (r["node"], r["via"]))

    # a named state is only worth naming if something can leave it
    from highvisor import plan
    r = plan.route(real, "qud", "stranded_stage", "title")
    check("there is a route OUT of a stranded stage", bool(r.get("ok")), str(r)[:160])


def dead_reporter():
    """A dead or stale first-party report must not be downgraded into a named screen.

    THE BUG, measured twice on 2026-08-07/08. `title`'s qud detector carried
    `{"game_live": false}` as an alternative. That is the absence of bytes on a 0.35s socket
    probe, and it is satisfied by EVERY menu screen — so whenever the report was stale or
    missing, `hv state` confidently named the title:

      * the mod failed to compile on a mid-tour restart, the heartbeat stopped, and the state
        read "Title Screen  via=live" for 7 minutes while Qud sat on the Modding Toolkit;
      * Qud parked on its Keybinds screen with a LIVE game behind it read the same way — a
        parked turn thread publishes no snapshot, so the probe is wrong about liveness itself.

    `gamego` PLANS from that answer, which is what made it expensive rather than cosmetic.

    Signals in, state out, so it belongs in SPOT. The `report` signal (fresh|stale|foreign|
    absent) is what carries the reason the file was refused, which previously vanished into a
    bare None and made "refused report" indistinguishable from "matched no scene".
    """
    real = gametree.load_tree()
    print("\ndead/stale reporter (no first-party state)")

    # 1. FRESH report -> unchanged behaviour, both directions
    r = gametree.evaluate(real, "qud", sig(scene="TitleScreen", report="fresh", game_live=False))
    check("a fresh report still resolves the title by SCENE",
          r["node"] == "title" and r["via"] == "scene", "%s via %s" % (r["node"], r["via"]))
    r = gametree.evaluate(real, "qud", sig(scene="play", report="fresh", game_live=True))
    check("a fresh report still resolves in-game", r["node"] == "in_game", str(r["node"]))

    # 2. THE BUG: no scene + no live game must NOT name the title
    for rep in ("stale", "absent", "foreign"):
        r = gametree.evaluate(real, "qud", sig(report=rep, game_live=False))
        check("report=%s + game_live false does NOT claim the title" % rep,
              r["node"] != "title", "read as %s via %s" % (r["node"], r["via"]))
        check("report=%s + game_live false is 'running, screen unknown'" % rep,
              r["node"] is None and r["running"] and not r["off"], str(r))

    # 3. the POSITIVE inference is deliberately kept: bytes imply a running game, and if the
    #    reporter is dead while a game runs this is the one signal still telling the truth
    r = gametree.evaluate(real, "qud", sig(report="stale", game_live=True))
    check("report=stale + game_live TRUE still resolves in-game",
          r["node"] == "in_game" and r["via"] == "live", "%s via %s" % (r["node"], r["via"]))

    # 4. a window with nothing to say at all is still off/unknown, not a screen
    r = gametree.evaluate(real, "qud", sig(present=False, report="absent"))
    check("no window -> off", r["off"] and not r["running"], str(r))

    # 5. the tree must not grow another inference-only detector
    INFER = {"game_live", "port", "present"}
    bad = []
    def walk(n):
        for app, cond in (n.get("detect") or {}).items():
            for c in (cond if isinstance(cond, list) else [cond]):
                keys = set(c) - {"note"}
                if keys and keys <= INFER and c.get("game_live") is not True:
                    bad.append("%s/%s %s" % (n.get("id"), app, c))
        for ch in n.get("children") or []:
            walk(ch)
    walk(real["root"])
    check("no detector rests on an inference alone (bar in_game's positive game_live)",
          not bad, "; ".join(bad))


def popup_conditions():
    """A `popup` condition that names ONE kind must not read as "no modal is up".

    THE BUG (measured live 2026-08-07). `raves in_game -> title` conditioned three of its
    four steps on `popup: "message"`. Qud's third quit confirm — the ABANDON AskString it
    raises for a Classic (non-checkpointing) save — mirrors into Raves as kind `input`, so
    every one of those conditions evaluated "not present", reported ok, and the route died
    25s later at its verify naming nothing. Raves reports FIVE kinds (message / menu /
    input / itempicker / feedback), so naming one leaves four that can step past.

    Two things are pinned here: `true`/`false`/list forms exist so a step can say "whatever
    is up" or "nothing is up" instead of guessing a kind, and the same matcher backs every
    caller (`hv assert`, an `assert` step, a `dismiss` condition) so they cannot drift
    apart again — the dismiss path used to compare strings only, which silently made
    `{"dismiss": {"popup": true}}` match nothing at all.
    """
    from highvisor.engine import Engine
    m = Engine._popup_matches

    print("\npopup conditions (kind-agnostic forms)")
    check("`true` matches ANY modal kind",
          m("input", True) and m("message", True) and m("itempicker", True))
    check("`true` does not match when nothing is up", not m("", True) and not m(None, True))
    check("`false` matches only when nothing is up",
          m("", False) and m(None, False) and not m("input", False))
    check("a named kind still matches itself", m("message", "message"))
    check("a named kind is CASE-insensitive", m("Message", "message"))
    check("a named kind does NOT match another kind — the ABANDON hole",
          not m("input", "message"))
    check("a LIST matches any member", m("menu", ["message", "menu"]))
    check("a LIST rejects a non-member", not m("input", ["message", "menu"]))

    # the assert path must go through the same matcher
    holds = Engine._assert_holds
    st_input = {"node": "in_game", "path": ["in_game"], "extra": {"popup": "input"}}
    st_clear = {"node": "title", "path": ["title"], "extra": {}}
    check("assert popup=true holds while an input modal is up",
          holds(None, {"popup": True}, st_input))
    check("assert popup=false REJECTS a lingering modal",
          not holds(None, {"popup": False}, st_input))
    check("assert popup=false passes once nothing is up",
          holds(None, {"popup": False}, st_clear))
    check("assert popup='message' rejects an input modal",
          not holds(None, {"popup": "message"}, st_input))


def mod_config_popup():
    """`loadsave` may answer ONE modal, matched on its text, chosen by the option's text.

    THE BUG THIS REPLACES (merged from the PC branch, 2026-08-08). The original fired
    `{"action":"option","index":1}` at whatever was up whenever `"popup" in scene.lower()`.
    Qud's heartbeat sets `scene` from the raw view name, so *every* Qud modal satisfies that
    test, and index 1 is a different button on a different modal. The specific hazard is
    named in the code: this popup's PRE-SELECTED option relaunches Qud with our bridge
    DISABLED, so guessing wrong here silently kills the mod for every later run.

    Tested against a FAKE BRIDGE rather than reasoned about, because the real popup only
    arises from a save whose mod configuration differs — a Lumpy-side condition that cannot
    be reproduced on this machine. The socket protocol is the mod's: 4-byte big-endian
    length, then JSON. The mod re-publishes the live popup to any joining client, which is
    why connecting is enough to read it.
    """
    import json as _json
    import socket as _socket
    import struct as _struct
    import threading

    from highvisor.engine import Engine

    def serve(frame):
        """One-shot bridge that announces `frame` and records what gets sent back."""
        srv = _socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        got = []

        def run():
            conn, _ = srv.accept()
            with conn:
                if frame is not None:
                    p = _json.dumps(frame).encode()
                    conn.sendall(_struct.pack(">I", len(p)) + p)
                conn.settimeout(2.0)
                buf = b""
                try:
                    while len(buf) < 4 or len(buf) < 4 + _struct.unpack(">I", buf[:4])[0]:
                        c = conn.recv(65536)
                        if not c:
                            break
                        buf += c
                except (OSError, _socket.timeout):
                    pass
                if len(buf) >= 4:
                    n = _struct.unpack(">I", buf[:4])[0]
                    if len(buf) >= 4 + n:
                        got.append(_json.loads(buf[4:4 + n]))
            srv.close()

        t = threading.Thread(target=run, daemon=True); t.start()
        return srv.getsockname()[1], got, t

    def ask(frame):
        port, got, t = serve(frame)
        # `self` is only used for the two class-level match constants, so the class
        # itself stands in for an instance -- no daemon, no backend, no app.
        r = Engine._answer_mod_config_popup(Engine, port)
        t.join(timeout=3)
        return r, (got[0] if got else None)

    MODCFG = {"type": "popup", "active": True, "kind": "menu",
              "message": "This save game was created with a different mod configuration.",
              "title": "Mod Configuration Differs",
              "options": [{"text": "Restart using save game's mod configuration"},
                          {"text": "Load keeping current mod configuration"}]}

    print("\nloadsave: the one modal it may answer")

    r, sent = ask(MODCFG)
    check("answers the Mod Configuration popup", r.get("answered"), repr(r))
    check("...by the option's LABEL, not a fixed index",
          r.get("chose") == "Load keeping current mod configuration", repr(r.get("chose")))
    check("...and sends that option's real position",
          sent == {"type": "command", "name": "popup", "action": "option", "index": 1},
          repr(sent))

    # THE POINT OF THE REWRITE: same popup, options in the other order. Index 1 is now the
    # bridge-disabling choice, so anything positional gets this exactly wrong.
    flipped = dict(MODCFG, options=list(reversed(MODCFG["options"])))
    r, sent = ask(flipped)
    check("follows the label when the options are reordered",
          r.get("chose") == "Load keeping current mod configuration", repr(r.get("chose")))
    check("...which is index 0 there, not 1", (sent or {}).get("index") == 0, repr(sent))

    # A DIFFERENT MODAL. The old code answered this one too.
    other = {"type": "popup", "active": True, "kind": "menu",
             "message": "Do you want to save first?", "title": "",
             "options": [{"text": "Yes"}, {"text": "No"}]}
    r, sent = ask(other)
    check("declines an unrelated modal", not r.get("answered"), repr(r))
    check("...sends it nothing at all", sent is None, repr(sent))
    check("...and names what it saw, so the failure is legible",
          "save first" in (r.get("saw") or ""), repr(r.get("saw")))

    # The right modal, but without the option we expect: refuse rather than fall back to a
    # position, because Qud's own default here is the one that disables the mod.
    r, sent = ask(dict(MODCFG, options=[{"text": "Restart using save game's mod configuration"}]))
    check("refuses the right modal when the safe option is absent", not r.get("answered"), repr(r))
    check("...sends nothing rather than guessing", sent is None, repr(sent))
    check("...and says why", "no 'keeping current' option" in (r.get("error") or "").lower()
          or "keeping current" in (r.get("error") or ""), repr(r.get("error")))

    # No modal up at all, and a dead bridge: both must be quiet non-answers.
    r, sent = ask(None)
    check("no modal up -> no answer, no error", not r.get("answered") and not r.get("saw"), repr(r))
    srv = _socket.socket(); srv.bind(("127.0.0.1", 0)); dead = srv.getsockname()[1]; srv.close()
    r = Engine._answer_mod_config_popup(Engine, dead)
    check("a closed bridge is reported, not raised", not r.get("answered") and r.get("error"),
          repr(r))


if __name__ == "__main__":
    sys.exit(main())
