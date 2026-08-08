"""hv — the command-line client for the highvisor daemon.

A thin, dependency-free wrapper over the framed-JSON protocol. It never imports a
backend; it just opens a socket to the daemon and prints what comes back. Any
other language could reimplement this in a few lines (that's the point).

    hv ping
    hv ls
    hv shot <target> [out.png] [--native]
    hv text <target> <string...>
    hv key <target> <keys> [--focus]
    hv click <target> <x> <y> [--right|--middle] [--double]
    hv activate <target>
    hv inspect <target> [depth]
    hv move <target> <zone | x y w h> [--topmost | --no-topmost]
    hv screen
    hv layouts
    hv layout <name>
    hv layout-save <name> [description...]
    hv diff <a.png> <b.png> [--out heat.png]
    hv zones <img.png> [--top N]
    hv peers
    hv parity <a> <b> [--peer-a NAME] [--peer-b NAME] [--out heat.png] [--size WxH]
    hv launch <name | spec>
    hv launchers
    hv launch-save <name> <spec>
    hv tunnel <host> [--user U] [--bridge] [--print]   (drive a remote highvisor over SSH)
    hv responsive <golem> <source> [--threshold P] [--out-dir DIR]
    hv raw '{"op":"ping"}'

<target> is a window ref: "hwnd:0x1a2b", "pid:1234", or a title substring.
"""
import argparse
import base64
import json
import os
import socket
import sys

from . import protocol as P


_HOST, _PORT = P.HOST, P.PORT  # set once from CLI args in main()


def _call(request: dict, timeout: float = 30.0) -> dict:
    # Long-running ops (a goto recipe walks menus + waits on asserts; an assert
    # polls up to its own --timeout) must outlive the socket read — pad past the
    # op's own budget so the daemon, not the client, is the one that gives up.
    if request.get("op") in ("gamego", "assert_state"):
        timeout = max(timeout, float(request.get("timeout", 0)) + 150.0)
    with socket.create_connection((_HOST, _PORT), timeout=timeout) as s:
        P.send_frame(s, request)
        resp = P.recv_frame(s)
    if resp is None:
        raise SystemExit("daemon closed the connection without replying")
    return resp


def _print_json(obj, strip=()):
    if isinstance(obj, dict) and strip:
        obj = {k: v for k, v in obj.items() if k not in strip}
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _cmd_ping(a):
    _print_json(_call({"op": P.OP_PING}))


def _screen_recording_warning(targets):
    """A one-line diagnosis for the failure that otherwise surfaces three layers away.

    Without the Screen Recording grant macOS does not refuse the window list — it returns it
    with every TITLE blanked, and captures come back empty. Downstream that reads as
    "no window for app 'raves'", or an OCR step reporting "text 'continue' not on screen"
    while looking straight at Continue. Several windows and not one title is the signature;
    say so HERE, where it is one fact, instead of leaving it to be rediscovered.
    """
    named = [t for t in targets if (t.get("title") or "").strip()]
    if len(targets) >= 3 and not named:
        return ("!! %d windows, NONE with a title — the daemon has no Screen Recording grant.\n"
                "   Captures and OCR will return nothing. Grant it to whatever runs the daemon:\n"
                "   System Settings > Privacy & Security > Screen Recording (the bundle is "
                "\"Highvisor\" if installed via `hv install-daemon`)." % len(targets))
    return None


def _cmd_ls(a):
    resp = _call({"op": P.OP_LIST})
    if not resp.get("ok"):
        _print_json(resp)
        return 1
    for t in resp.get("targets", []):
        mark = "*" if t.get("focused") else " "
        print("%s %-16s pid=%-6d %4dx%-4d  %s"
              % (mark, t["id"], t["pid"], t["w"], t["h"], t["title"]))
    warn = _screen_recording_warning(resp.get("targets", []))
    if warn:
        print("\n" + warn)


def _cmd_shot(a):
    resp = _call({"op": P.OP_SHOT, "target": a.target, "native": a.native,
                  "live": getattr(a, "live", False),
                  "live_age": getattr(a, "live_age", None),
                  "live_timeout": getattr(a, "live_timeout", None)})
    if not resp.get("ok"):
        _print_json(resp)
        return 1
    out = a.out or "shot.png"
    with open(out, "wb") as f:
        f.write(base64.b64decode(resp["png_b64"]))
    dims = ""
    w, h = resp.get("w"), resp.get("h")
    if w and h:
        # Report the pixel size so you can tell a 1x (point-size, click-1:1)
        # capture from a 2x Retina one at a glance — on a 2x shot a coordinate
        # read off the PNG must be halved before you click it (see the
        # click-coordinate note in the ops quickref).
        dims = " %dx%d px%s" % (w, h, " (native/backing)" if a.native else "")
    live = ""
    if resp.get("live_checked"):
        live = "  [live ui_age=%s%s]" % (
            resp.get("ui_age"),
            "" if resp.get("live_tries") in (0, None) else ", %d activate(s)" % resp["live_tries"])
    elif getattr(a, "live", False) and resp.get("live_reason"):
        live = "  [--live skipped: %s]" % resp["live_reason"]
    print("wrote %s (%d bytes)%s%s" % (out, resp.get("bytes", 0), dims, live))
    # A capture the app was not rendering is worse than no capture, because it looks
    # fine: Qud hands back its last frame, which for a status screen is the bare
    # playfield. Write it anyway so it can be inspected, but fail so a script stops.
    if resp.get("live_checked") and not resp.get("live"):
        sys.stderr.write("STALE CAPTURE: %s\n" % resp.get("live_reason", "app not rendering"))
        return 1


def _cmd_text(a):
    _print_json(_call({"op": P.OP_TEXT, "target": a.target,
                       "text": " ".join(a.text)}))


def _cmd_key(a):
    _print_json(_call({"op": P.OP_KEY, "target": a.target, "keys": a.keys,
                       "focus": a.focus}))


def _button(a):
    """--right/--middle pick the button; left is the default."""
    if getattr(a, "middle", False):
        return "middle"
    return "right" if a.right else "left"


def _cmd_click(a):
    _print_json(_call({"op": P.OP_CLICK, "target": a.target, "x": a.x, "y": a.y,
                       "button": _button(a), "double": a.double,
                       "hover": a.hover, "modifiers": a.mod}))


def _cmd_mouse(a):
    """Warp + a real mouseMoved, NO buttons — the tool for capturing hover states."""
    _print_json(_call({"op": P.OP_MOUSE, "target": a.target, "x": a.x, "y": a.y}))


def _cmd_drag(a):
    _print_json(_call({"op": P.OP_DRAG, "target": a.target,
                       "x1": a.x1, "y1": a.y1, "x2": a.x2, "y2": a.y2,
                       "button": _button(a),
                       "steps": a.steps, "modifiers": a.mod, "hold": a.hold}))


def _cmd_activate(a):
    _print_json(_call({"op": P.OP_ACTIVATE, "target": a.target}))


def _cmd_inspect(a):
    _print_json(_call({"op": P.OP_INSPECT, "target": a.target, "depth": a.depth}))


def _cmd_move(a):
    req = {"op": P.OP_MOVE, "target": a.target, "topmost": a.topmost}
    if len(a.rect) == 1:
        req["zone"] = a.rect[0]
    elif len(a.rect) == 4:
        req["x"], req["y"], req["w"], req["h"] = (int(v) for v in a.rect)
    else:
        raise SystemExit("move needs either a zone name or x y w h")
    _print_json(_call(req))


def _cmd_stack(a):
    _print_json(_call({"op": P.OP_STACK, "top": a.top, "bottom": a.bottom, "gap": a.gap}))


def _cmd_dock(a):
    _print_json(_call({"op": P.OP_DOCK, "target": a.target}))


def _cmd_probe(a):
    req = {"op": P.OP_PROBE}
    if a.app:
        req["app"] = a.app
    if a.window:
        req["window"] = a.window
    if a.port is not None:
        req["port"] = a.port
    _print_json(_call(req))


def _cmd_screen(a):
    _print_json(_call({"op": P.OP_SCREEN}))


def _cmd_state(a):
    """One-line-per-app live state (the state tree's evaluator, human-readable)."""
    res = _call({"op": P.OP_GAMESTATE, **({"ocr": True} if a.ocr else {})})
    if not res.get("ok"):
        _print_json(res)
        raise SystemExit(1)
    for app, st in (res.get("states") or {}).items():
        sig = st.get("signals") or {}
        extra = st.get("extra") or {}
        bits = [f"{app:6s}", "off" if st.get("off") else (st.get("label") or "?")]
        if sig.get("scene"):
            bits.append("scene=%s" % sig["scene"])
        if extra.get("popup"):
            bits.append("popup=%s" % extra["popup"])
        if extra.get("mode"):
            bits.append("mode=%s" % extra["mode"])
        bits.append("via=%s" % st.get("via"))
        # A REFUSED report is said out loud. It used to vanish, and the fallback inference
        # then named a screen we had no evidence for -- the operator saw a confident answer
        # with nothing marking it as a guess.
        rep = sig.get("report")
        if rep and rep != "fresh":
            bits.append("!! report=%s (no first-party state; not guessing a screen)" % rep)
        # A leaked second instance is the one condition that makes every OTHER field
        # here untrustworthy (we may drive one window and read another), so it is shouted
        # rather than tucked into --json.
        if (sig.get("instances") or 0) > 1:
            bits.append("!! %d INSTANCES — `hv restart %s`" % (sig["instances"], app))
        print("  ".join(str(b) for b in bits))


def _cmd_assert(a):
    """TDD assert: block until the condition holds (exit 0) or times out (exit 1)."""
    req = {"op": P.OP_ASSERT, "app": a.app, "timeout": a.timeout}
    if a.node:
        req["node"] = a.node
    if a.scene:
        req["scene"] = a.scene
    if a.popup is not None:
        req["popup"] = True if a.popup == "" else a.popup
    if a.present is not None:
        req["present"] = a.present == "yes"
    if a.ocr_contains:
        req["ocr_contains"] = a.ocr_contains
    res = _call(req)
    _print_json(res)
    raise SystemExit(0 if res.get("ok") and res.get("passed") else 1)


def _cmd_goto(a):
    """Drive an app to a state-tree node along a planned route."""
    res = _call({"op": P.OP_GAMEGO, "app": a.app, "node": a.node,
                 "no_restart": bool(getattr(a, "no_restart", False))})
    if res.get("route"):
        print("route: %s" % res["route"])
    # A restart is loud on the way past, not something to find later in the JSON.
    for st in res.get("steps") or []:
        if "restart_planned" in (st.get("step") or {}):
            print("!! %s" % st.get("detail"))
    _print_json(res)
    raise SystemExit(0 if res.get("ok") else 1)


def _cmd_plan(a):
    """Show the route `hv goto` WOULD take — nothing is driven.

    Use it before a long run, and to check a route for a screen you are not on:
    `hv plan raves status_skills --from off` answers "what happens if I ask for this
    with nothing running", without launching anything.
    """
    req = {"op": P.OP_PLAN, "app": a.app, "node": a.node}
    if a.frm:
        req["from"] = a.frm
    res = _call(req)
    if not res.get("ok"):
        print("no route: %s" % res.get("error"))
        raise SystemExit(1)
    print("%s" % res.get("summary"))
    if a.node is None:
        # bulk mode: every reachable state and what it would cost, cheapest first
        for node, cost in sorted((res.get("costs") or {}).items(), key=lambda kv: (kv[1], kv[0])):
            print("  %-6s %s" % (cost, node))
        raise SystemExit(0)
    for i, e in enumerate(res.get("steps") or [], 1):
        print("  %d. -> %-22s cost %-4s %s" % (
            i, e.get("to"), e.get("cost"),
            " ".join(sorted(k for s in (e.get("steps") or []) for k in s
                            if k not in ("window", "note", "args")))))
    raise SystemExit(0)


def _cmd_scroll(a):
    """Wheel event at a window point, e.g. `hv scroll raves 960 540 --dy 1 --mod ctrl`."""
    res = _call({"op": P.OP_SCROLL, "target": a.target, "x": a.x, "y": a.y,
                 "dy": a.dy, "dx": a.dx, "modifiers": a.mod or ""})
    _print_json(res)
    raise SystemExit(0 if res.get("ok") else 1)


def _cmd_wish(a):
    """Run a Caves of Qud wish through the Raves mod bridge (godmode, item:..., xp:...)."""
    res = _call({"op": P.OP_QUDWISH, "wish": " ".join(a.text)})
    _print_json(res)
    raise SystemExit(0 if res.get("ok") else 1)


def _cmd_saves(a):
    """The save list + picker row order, read from DISK (no game launch)."""
    res = _call({"op": P.OP_QUD_SAVES})
    if not res.get("ok"):
        _print_json(res)
        raise SystemExit(1)
    for s in res["saves"]:
        print("row %d: %-26r %-8s %-24s saved %s" % (
            s["row"], s["name"], s["mode"], s["location"], s["saved"]))


def _cmd_loadsave(a):
    """Load a NAMED save (row computed from disk metadata — no top-row roulette)."""
    res = _call({"op": P.OP_LOAD_SAVE, "name": " ".join(a.name)}, timeout=180.0)
    _print_json(res)
    raise SystemExit(0 if res.get("ok") else 1)


def _cmd_quit(a):
    """Stop an app and LEAVE it stopped — the gap between `launch` and `restart`.

    Wanted whenever a test needs one app alone: these two mirror each other's modals over
    the bridge, so "does this still fail with only Qud up?" is a real question, and before
    this the only way to ask it was hand-driving the app's own quit menu.
    """
    res = _call({"op": P.OP_QUIT, "app": a.app, "force": bool(a.force)}, timeout=40.0)
    _print_json(res)
    raise SystemExit(0 if res.get("ok") else 1)


def _cmd_restart(a):
    """Clean restart: kill EVERY instance (duplicates too), launch solo, wait for the window."""
    res = _call({"op": P.OP_RESTART, "app": a.app}, timeout=120.0)
    _print_json(res)
    raise SystemExit(0 if res.get("ok") else 1)


def _cmd_trace(a):
    """The last N goto runs, one line each: what it steered by -> what it reached.

    Reads like a flight recorder, which is the point -- a goto that fails is usually
    only diagnosable AFTER the fact, and the interesting field is `entry`: the state
    the recipe believed it was starting from.
    """
    res = _call({"op": P.OP_TRACE, "limit": a.limit})
    if a.json:
        _print_json(res)
        raise SystemExit(0 if res.get("ok") else 1)
    runs = res.get("runs") or []
    if not runs:
        print("no goto runs recorded yet (%s)" % res.get("path"))
        return
    for r in runs:
        entry = (r.get("entry") or {}).get("node") or "?"
        exit_ = (r.get("exit") or {}).get("node") or "?"
        mark = "ok " if r.get("ok") else "FAIL"
        nsteps = len(r.get("steps") or [])
        line = "%s  %s  %-6s %-18s %s -> %s  (%d steps)" % (
            r.get("t", ""), mark, r.get("app", ""), r.get("node", ""), entry, exit_, nsteps)
        if r.get("detail"):
            line += "  [%s]" % r["detail"]
        print(line)
        if not r.get("ok"):
            print("      error: %s" % r.get("error"))
            for st in (r.get("steps") or []):
                if not st.get("ok"):
                    print("      failed step: %s" % st.get("step"))


def _cmd_test(a):
    """Run a REGISTERED check by id, or list them all with no arguments."""
    if not a.test:
        from . import gametree
        for node, t in gametree.all_tests():
            print("%-16s %-20s %-5s %s" % (node or "(harness)", t["id"], t.get("tier", "?"), t["cmd"]))
        raise SystemExit(0)
    res = _call({"op": P.OP_RUN_TEST, "node": a.node or "", "test": a.test}, timeout=660.0)
    if not res.get("ok") and res.get("have"):
        print("no such test. registered:")
        for h in res["have"]:
            print("  " + h)
        raise SystemExit(1)
    print("%s (%s) — %s" % (res.get("test"), res.get("tier"), res.get("detail", res.get("error"))))
    for ln in res.get("tail") or []:
        print("  " + ln)
    raise SystemExit(0 if res.get("ok") else 1)


def _cmd_grant_input(a):
    """Raise the system Accessibility prompt for the DAEMON process, and say which identity
    macOS is actually asking about — it is the interpreter, not Highvisor.app (see the
    backend docstring; the two TCC grants resolve differently)."""
    res = _call({"op": P.OP_GRANT_INPUT})
    _print_json(res)
    if res.get("process"):
        print("\nmacOS keys ACCESSIBILITY to this binary:\n  %s" % res["process"])
        print("Approve the dialog, or tick that entry under\n"
              "  System Settings > Privacy & Security > Accessibility\n"
              "then re-run `hv install-daemon` to confirm both grants.")
    raise SystemExit(0 if res.get("trusted") else 1)


def _cmd_abort(a):
    """Panic: release focus/mouse NOW; refuse control ops for 30s."""
    _print_json(_call({"op": P.OP_ABORT}))


def _cmd_install_daemon(a):
    """Write + bootstrap the launchd KeepAlive agent so the daemon restarts itself on crash
    (code changes already re-exec in place).

    Runs it through **Highvisor.app**, and that is the whole point of the bundle. macOS
    attributes Screen Recording to a "responsible process": a daemon started from a terminal
    inherits the terminal's grant, and the identical binary started by launchd does not — so
    the first version of this command traded screen capture for crash survival, and the
    symptom was three layers away (blank window titles -> "no window for app 'raves'" ->
    "text 'continue' not on screen"). The bundle gives macOS ONE thing to grant that survives
    venv rebuilds, Python upgrades and every source edit. Built here if missing.
    """
    import os
    import plistlib
    import subprocess
    import sys
    import time
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = os.path.join(repo, "build", "Highvisor.app")
    exe = os.path.join(app, "Contents", "MacOS", "Highvisor")
    if not os.path.exists(exe):
        print("building %s …" % app)
        b = subprocess.run([os.path.join(repo, "tools", "make_app.sh")],
                           capture_output=True, text=True)
        print((b.stdout or "").rstrip())
        if b.returncode != 0:
            print("could not build the app bundle:\n%s" % (b.stderr or "").rstrip())
            print("falling back to the bare interpreter — expect NO screen capture under launchd")
    program = [exe] if os.path.exists(exe) else [sys.executable, "-m", "highvisor.server"]

    label = "com.highvisor.daemon"
    plist = {
        "Label": label,
        "ProgramArguments": program,
        "WorkingDirectory": repo,
        "KeepAlive": True,
        "RunAtLoad": True,
        "StandardOutPath": os.path.expanduser("~/Library/Logs/highvisor.log"),
        "StandardErrorPath": os.path.expanduser("~/Library/Logs/highvisor.log"),
    }
    path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    uid = os.getuid()
    # A manually-run daemon would hold port 48720 and the launchd job would die on every
    # restart attempt — which looks exactly like a broken agent. Clear it first rather than
    # telling the user about it afterwards.
    subprocess.run(["pkill", "-f", "highvisor.server"], capture_output=True)
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)], capture_output=True)
    time.sleep(1.5)
    r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, path],
                       capture_output=True, text=True)
    print("plist:   %s" % path)
    print("program: %s" % " ".join(program))
    print("bootstrap: %s" % ("ok" if r.returncode == 0 else (r.stderr.strip() or "failed")))
    print("logs -> ~/Library/Logs/highvisor.log")
    if r.returncode != 0:
        return 1

    # VERIFY, do not assume. The agent can be running perfectly and still be unable to see a
    # single window title, which is the failure this whole bundle exists to make fixable.
    # RETRY THROUGH THE GAP. launchd has bootstrapped the job but the daemon needs a second or
    # two to bind 48720, and `_call` RAISES on a refused connection rather than returning — so
    # the first, expected refusal escaped this loop entirely and the install reported failure
    # for a daemon that was about to come up fine.
    resp = {}
    for _ in range(12):
        time.sleep(1.0)
        try:
            resp = _call({"op": P.OP_LIST})
        except (OSError, SystemExit):
            continue
        if resp.get("ok"):
            break
    else:
        print("\n!! the agent is bootstrapped but the daemon is not answering on 48720 — "
              "check the log")
        return 1
    problems = []
    warn = _screen_recording_warning(resp.get("targets", []))
    if warn:
        problems.append(("Screen Recording", warn))
    else:
        print("\nscreen capture:  OK (%d windows, titles readable)" % len(resp.get("targets", [])))

    # ACCESSIBILITY IS A SEPARATE GRANT, and its absence is far nastier than Screen
    # Recording's: CGEventPost does not fail without it, so every click and keypress returns
    # ok:true and goes nowhere. Checking capture alone once let an install pass while the
    # harness could not drive a single thing — an hour went into suspecting the app.
    ax = _call({"op": P.OP_INSPECT, "target": (resp.get("targets") or [{}])[0].get("id", ""),
                "depth": 1})
    if "Accessibility permission" in str(ax.get("error", "")):
        problems.append(("Accessibility",
            "!! synthetic input is DEAD — the daemon has no Accessibility grant.\n"
            "   click/key/scroll will report success and do nothing (CGEventPost does not\n"
            "   fail without it). System Settings > Privacy & Security > Accessibility."))
    else:
        print("synthetic input: OK (Accessibility granted)")

    if problems:
        print()
        for _, text in problems:
            print(text)
        print("\n   The bundle is registered, so \"Highvisor\" should be listed under %s.\n"
              "   Enable it, then re-run `hv install-daemon` — it boots the agent out and back\n"
              "   in, which is what makes a new grant take effect."
              % " AND ".join(name for name, _ in problems))
        return 1


def _cmd_diff(a):
    # Local image analysis — no daemon round-trip.
    from . import imageops
    res = imageops.diff(a.a, a.b, crop_top=a.crop_top, out=a.out)
    if a.regions:   # add a ranked "where do they diverge" punch-list + annotated image
        gx, gy = (int(v) for v in a.grid.lower().split("x"))
        ann = a.regions_out or (a.out + ".regions.png" if a.out else None)
        res["regions"] = imageops.regions(a.a, a.b, crop_top=a.crop_top, grid=(gx, gy),
                                          threshold=a.threshold, out=ann)
    _print_json(res)


def _cmd_zones(a):
    from . import imageops
    z = imageops.detect_zones(a.img, top=a.top)
    _print_json({"count": len(z), "zones": z})


def _cmd_responsive(a):
    # Orchestrates the daemon (screen/move/shot) + local diff; see responsive.py.
    from . import responsive
    report = responsive.run(_call, a.golem, a.source,
                            threshold=a.threshold, out_dir=a.out_dir,
                            display=a.display)
    _print_json(report)
    return 0 if report["verdict"] == "PASS" else 1


def _cmd_layouts(a):
    resp = _call({"op": P.OP_LAYOUT_LIST})
    if not resp.get("ok"):
        _print_json(resp)
        return 1
    for l in resp.get("layouts", []):
        print("%-14s %2d  %s" % (l["name"], l["placements"], l.get("description", "")))


def _cmd_layout(a):
    resp = _call({"op": P.OP_LAYOUT_APPLY, "name": a.name})
    if not resp.get("ok") and "results" not in resp:
        _print_json(resp)
        return 1
    for r in resp.get("results", []):
        mark = "ok " if r.get("ok") else "MISS"
        print("  %s %-12s %s %s" % (mark, r.get("match", ""),
                                    r.get("title", r.get("target", "")),
                                    r.get("error") or ""))
    print("%d/%d placed" % (resp.get("applied", 0), len(resp.get("results", []))))
    return 0 if resp.get("ok") else 1


def _cmd_layout_save(a):
    _print_json(_call({"op": P.OP_LAYOUT_SAVE, "name": a.name,
                       "description": " ".join(a.description) if a.description else ""}))


def _cmd_launch(a):
    _print_json(_call({"op": P.OP_LAUNCH, "name": a.name}))


def _cmd_launchers(a):
    resp = _call({"op": P.OP_LAUNCH_LIST})
    if not resp.get("ok"):
        _print_json(resp)
        return 1
    launchers = resp.get("launchers") or {}
    for n, s in launchers.items():
        print("%-14s %s" % (n, s))
    if not launchers:
        print("(no launchers saved — hv launch-save <name> <spec>)")


def _cmd_launch_save(a):
    _print_json(_call({"op": P.OP_LAUNCH_SAVE, "name": a.name, "spec": a.spec}))


def _cmd_ocr(a):
    resp = _call({"op": P.OP_OCR, "target": a.target})
    if not resp.get("ok"):
        _print_json(resp)
        return 1
    if a.boxes:
        _print_json(resp)
    else:
        print(resp.get("text", ""))


def _cmd_peers(a):
    resp = _call({"op": P.OP_PEERS})
    if not resp.get("ok"):
        _print_json(resp)
        return 1
    me = resp.get("self") or {}
    print("self: %s@%s:%s" % (me.get("name"), me.get("host"), me.get("port")))
    for p in resp.get("peers", []):
        print("  %-16s %s:%s" % (p["name"], p["host"], p["port"]))
    if not resp.get("peers"):
        print("  (no peers discovered yet)")


def _capture(target, peer, path):
    """Write a PNG of ``target`` — from a peer over the bridge if ``peer`` set,
    else from the local daemon."""
    if peer:
        r = _call({"op": P.OP_PEER_SHOT, "peer": peer, "target": target})
    else:
        r = _call({"op": P.OP_SHOT, "target": target})
    if not r.get("ok") or not r.get("png_b64"):
        raise SystemExit("capture %s%s failed: %s"
                         % (peer + ":" if peer else "", target, r.get("error")))
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["png_b64"]))
    return r.get("bytes", 0)


def _find_target(ref):
    """The current Target dict for a ref (id, or title/owner substring), or None."""
    low = ref.lower()
    for t in _call({"op": P.OP_LIST}).get("targets", []):
        if (t["id"] == ref or low in (t.get("title") or "").lower()
                or low in (t.get("class_name") or "").lower()):
            return t
    return None


def _resize_local(ref, w, h):
    """Resize a local window to w x h in place (keeps its current origin)."""
    t = _find_target(ref)
    if not t:
        raise SystemExit("resize: no local window matching %r" % ref)
    _call({"op": P.OP_MOVE, "target": t["id"], "x": t["x"], "y": t["y"],
           "w": w, "h": h, "topmost": False})


def _cmd_parity(a):
    """One-shot visual parity: capture A and B (each local or from a peer) and
    screenshot-diff them with imageops — no LLM in the loop."""
    import tempfile
    import time
    from . import imageops
    if a.size:
        try:
            sw, sh = (int(v) for v in a.size.lower().split("x"))
        except ValueError:
            raise SystemExit("--size must be WxH, e.g. 1920x1080")
        if not a.peer_a:
            _resize_local(a.a, sw, sh)
        if not a.peer_b:
            _resize_local(a.b, sw, sh)
        time.sleep(a.settle)   # let both apps repaint at the new size
    d = tempfile.mkdtemp(prefix="hv-parity-")
    ap, bp = os.path.join(d, "a.png"), os.path.join(d, "b.png")
    _capture(a.a, a.peer_a, ap)
    _capture(a.b, a.peer_b, bp)
    heat = a.out or os.path.join(d, "heat.png")
    res = imageops.diff(ap, bp, crop_top=a.crop_top, out=heat)
    res["a"] = (a.peer_a + ":" if a.peer_a else "") + a.a
    res["b"] = (a.peer_b + ":" if a.peer_b else "") + a.b
    res["captures"] = [ap, bp]
    _print_json(res)


def _ocr_words(res, minlen=4):
    """Set of distinctive words (>= minlen, lowercased, alphanumeric) from an OCR result.
    Word-level is robust to the fact that Vision segments the same text into DIFFERENT boxes
    on two captures of stylized fonts — line-level set-diff can't align those, words can."""
    import re
    words = set()
    for b in (res.get("boxes") or []):
        for w in re.sub(r"[^a-z0-9]+", " ", (b.get("text") or "").lower()).split():
            if len(w) >= minlen:
                words.add(w)
    return words


def _text_diff(a_target, b_target, thr=0.8):
    """OCR both windows and word-level diff — the SEMANTIC layer pixel diff can't isolate:
    which content words appear on the reference but not the current, and vice versa. Reports
    `coverage` (% of the reference's distinctive words fuzzily present in the current) plus the
    `missing` (reference-only) and `extra` (current-only) word lists. Still OCR-rough on
    stylized fonts — read `missing` as a candidate checklist of content the current lacks."""
    import difflib
    wa = _ocr_words(_call({"op": P.OP_OCR, "target": a_target}))
    wb = _ocr_words(_call({"op": P.OP_OCR, "target": b_target}))

    def present(w, pool):
        return w in pool or any(difflib.SequenceMatcher(None, w, p).ratio() >= thr for p in pool)

    missing = sorted(w for w in wa if not present(w, wb))
    extra = sorted(w for w in wb if not present(w, wa))
    covered = len(wa) - len(missing)
    coverage = round(100.0 * covered / max(1, len(wa)), 1)
    return {"coverage": coverage, "missing": missing, "extra": extra,
            "counts": {"reference_words": len(wa), "current_words": len(wb), "covered": covered}}


def _cmd_text_diff(a):
    d = _text_diff(a.a, a.b, thr=a.threshold)
    d["a"], d["b"] = a.a, a.b
    _print_json(d)


def _run_steps(window, steps, cwd=None):
    import time
    import subprocess
    for st in steps or []:
        if "shell" in st:
            # Run a command before capturing — the hook for data setup (e.g. loading an option preset
            # so a scene captures a deterministic config). argv list (no shell=True) or a string to split;
            # cwd defaults to the scene config's directory so relative tool paths resolve.
            cmd = st["shell"]
            if isinstance(cmd, str):
                import shlex
                cmd = shlex.split(cmd)
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=float(st.get("timeout", 60)))
            if r.returncode != 0:
                raise SystemExit("scene shell step failed (%s): %s" % (cmd, (r.stderr or r.stdout).strip()))
        elif "move" in st:
            x, y, w, h = st["move"]
            _call({"op": P.OP_MOVE, "target": window, "x": int(x), "y": int(y),
                   "w": int(w), "h": int(h), "topmost": False})
        elif "click" in st:
            x, y = st["click"]
            req = {"op": P.OP_CLICK, "target": window, "x": int(x), "y": int(y),
                   "button": st.get("button", "left"), "double": bool(st.get("double", False))}
            if st.get("hover"):
                req["hover"] = True
            _call(req)
        elif "key" in st:
            _call({"op": P.OP_KEY, "target": window, "keys": st["key"], "focus": st.get("focus", True)})
        elif "wait" in st:
            time.sleep(float(st["wait"]))


def _drive_capture(spec, out_path, settle, cwd=None):
    """Resize (if `size`), run `reset` then `steps`, settle, and capture the window.
    `cwd` is the working dir for any `shell` steps (the scene config's directory)."""
    import time
    win = spec["window"]
    if spec.get("size"):
        sw, sh = (int(v) for v in str(spec["size"]).lower().split("x"))
        _resize_local(win, sw, sh)
    _run_steps(win, spec.get("reset"), cwd=cwd)
    _run_steps(win, spec.get("steps"), cwd=cwd)
    time.sleep(settle)
    _capture(win, None, out_path)


def _cmd_scene(a):
    """Drive a window to a named UI state, then one of three modes:
      • default  — regression-diff the capture vs its stored golden (pass/fail + regions);
      • --bless  — write the capture AS the golden (establish/update the reference);
      • --parity — also drive the scene's `reference` window (e.g. Qud) to the same screen
                   and diff the two LIVE captures (parity match% + regions + side-by-side).
    Scenes run from a known start (a fresh app / Qud at its title); `reset` steps normalize."""
    import os
    import shutil
    from . import imageops
    from . import scenes as scenes_mod
    scenes = scenes_mod.load(a.config)
    names = ([k for k, v in scenes.items() if isinstance(v, dict)] if a.all
             else ([a.name] if a.name else []))
    if not names:
        raise SystemExit("give a scene name or --all")
    cfg_dir = os.path.dirname(os.path.abspath(a.config))   # working dir for scenes' `shell` steps
    out_dir = a.out or os.path.join(cfg_dir, "_regress")
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for nm in names:
        sc = scenes.get(nm)
        if sc is None:
            results.append({"scene": nm, "error": "not in config"})
            continue
        cur = os.path.join(out_dir, nm + ".png")
        _drive_capture(sc, cur, a.settle, cwd=cfg_dir)

        if a.parity:                                   # diff vs a LIVE reference window (Qud)
            ref_spec = sc.get("reference")
            if not ref_spec:
                results.append({"scene": nm, "error": "no `reference` block for --parity", "current": cur})
                continue
            ref = os.path.join(out_dir, nm + "_ref.png")
            _drive_capture(ref_spec, ref, a.settle, cwd=cfg_dir)
            ct = int(ref_spec.get("crop_top", sc.get("crop_top", 58)))
            d = imageops.diff(ref, cur, crop_top=ct)
            ann = os.path.join(out_dir, nm + "_parity.png")
            reg = imageops.regions(ref, cur, crop_top=ct, out=ann)
            sbs = os.path.join(out_dir, nm + "_sbs.png")
            imageops.sidebyside(ref, cur, sbs, label_a=ref_spec.get("window", "reference"),
                                label_b=sc.get("window", "current"))
            thr = float(sc.get("parity_threshold", 90.0))
            entry = {"scene": nm, "mode": "parity", "parity_match": d["content_match"],
                     "pass": d["content_match"] >= thr, "threshold": thr,
                     "worst": reg["worst"][:5], "reference": ref, "current": cur,
                     "sidebyside": sbs, "diff": ann}
            if a.text:   # also OCR both live windows + diff their text (label/wording gaps)
                entry["text"] = _text_diff(ref_spec["window"], sc["window"])
            results.append(entry)
            continue

        golden = scenes_mod.rel(a.config, sc.get("golden", "golden/" + nm + ".png"))
        if a.bless:
            os.makedirs(os.path.dirname(golden), exist_ok=True)
            shutil.copy(cur, golden)
            results.append({"scene": nm, "blessed": golden})
            continue
        if not os.path.exists(golden):
            results.append({"scene": nm, "error": "no golden — run with --bless first", "current": cur})
            continue
        ct = int(sc.get("crop_top", 58))
        d = imageops.diff(golden, cur, crop_top=ct)
        ann = os.path.join(out_dir, nm + "_diff.png")
        reg = imageops.regions(golden, cur, crop_top=ct, out=ann)
        thr = float(sc.get("threshold", 98.0))
        results.append({"scene": nm, "content_match": d["content_match"],
                        "pass": d["content_match"] >= thr, "threshold": thr,
                        "worst": reg["worst"][:5], "current": cur, "golden": golden, "diff": ann})
    scored = [r for r in results if "pass" in r]
    _print_json({"passed": sum(1 for r in scored if r["pass"]), "of": len(scored), "results": results})


def _parse_sizes(spec):
    out = []
    for tok in spec.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        try:
            w, h = (int(v) for v in tok.split("x"))
        except ValueError:
            raise SystemExit("--sizes items must be WxH, got %r" % tok)
        out.append((w, h))
    if not out:
        raise SystemExit("--sizes is empty")
    return out


def _cmd_parity_sweep(a):
    """Resize A and B through several window sizes TOGETHER, capturing + diffing each,
    to see how a reconstruction tracks the source across shapes — e.g. Raves' menu vs
    Qud's own at 16:9 / square / portrait / ultrawide. Both sides should be showing
    the comparable screen (put Qud at its title). Writes per-size side-by-sides, diff
    heatmaps + scores, and one stacked sweep.png. Restores original sizes when done."""
    import os
    import time
    import tempfile
    from . import imageops
    sizes = _parse_sizes(a.sizes)
    outdir = a.out or tempfile.mkdtemp(prefix="hv-sweep-")
    os.makedirs(outdir, exist_ok=True)
    orig = {}
    if not a.peer_a:
        orig["a"] = _find_target(a.a)
    if not a.peer_b:
        orig["b"] = _find_target(a.b)
    results, cmps = [], []
    for (w, h) in sizes:
        if not a.peer_a:
            _resize_local(a.a, w, h)
        if not a.peer_b:
            _resize_local(a.b, w, h)
        time.sleep(a.settle)                       # let both apps relayout + repaint
        tag = "%dx%d" % (w, h)
        ap = os.path.join(outdir, tag + "_a.png")
        bp = os.path.join(outdir, tag + "_b.png")
        _capture(a.a, a.peer_a, ap)
        _capture(a.b, a.peer_b, bp)
        heat = os.path.join(outdir, tag + "_heat.png")
        d = imageops.diff(ap, bp, crop_top=a.crop_top, out=heat)
        worst = None
        if a.regions:   # per-size punch-list: where do they diverge at this shape
            worst = imageops.regions(ap, bp, crop_top=a.crop_top,
                                     out=os.path.join(outdir, tag + "_regions.png"))["worst"][:5]
        cmp_path = os.path.join(outdir, tag + "_cmp.png")
        imageops.sidebyside(ap, bp, cmp_path, label_a="%s  %s" % (a.a, tag),
                            label_b="%s  %s" % (a.b, tag), height=a.height)
        cmps.append(cmp_path)
        entry = {"size": [w, h], "content_match": d["content_match"],
                 "full_match": d["full_match"], "compare": cmp_path, "heatmap": heat}
        if worst is not None:
            entry["worst"] = worst
        results.append(entry)
    sheet = os.path.join(outdir, "sweep.png")
    imageops.stack_vertical(cmps, sheet)
    if not a.no_restore:                           # put both windows back as they were
        for side, ref in (("a", a.a), ("b", a.b)):
            t = orig.get(side)
            if t:
                _call({"op": P.OP_MOVE, "target": t["id"], "x": t["x"], "y": t["y"],
                       "w": t["w"], "h": t["h"], "topmost": False})
    _print_json({"out": outdir, "sheet": sheet, "sizes": results})


def _cmd_tunnel(a):
    """SSH-tunnel a remote highvisor to this machine: forward the remote daemon +
    cockpit (+ optional bridge) to local ports over an encrypted SSH connection.
    Reuses the whole daemon/CLI unchanged — only the wire becomes SSH."""
    host = "%s@%s" % (a.user, a.host) if a.user else a.host
    fwds = [(a.control, P.PORT), (a.web, 48721)]          # remote 48721 = cockpit
    if a.bridge:
        fwds.append((a.bridge_port, P.BRIDGE_PORT))
    ssh = ["ssh", "-N"]
    for lp, rp in fwds:
        ssh += ["-L", "127.0.0.1:%d:127.0.0.1:%d" % (lp, rp)]
    ssh.append(host)
    print("→ tunnelling %s's highvisor here (encrypted). Needs sshd + key auth on it." % a.host)
    print("    control : hv --port %d <cmd>   (e.g. hv --port %d ls)" % (a.control, a.control))
    print("    cockpit : http://127.0.0.1:%d" % a.web)
    if a.bridge:
        print("    bridge  : 127.0.0.1:%d" % a.bridge_port)
    print("    ssh     : %s" % " ".join(ssh))
    if a.print_only:
        return
    print("  (holds the tunnel open until Ctrl-C)")
    os.execvp("ssh", ssh)


def _cmd_raw(a):
    _print_json(_call(json.loads(a.json)), strip=("png_b64",))


def build_parser():
    p = argparse.ArgumentParser(prog="hv", description="highvisor CLI client")
    p.add_argument("--host", default=P.HOST)
    p.add_argument("--port", type=int, default=P.PORT)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping").set_defaults(fn=_cmd_ping)
    sub.add_parser("ls").set_defaults(fn=_cmd_ls)

    s = sub.add_parser("shot")
    s.add_argument("target")
    s.add_argument("out", nargs="?")
    s.add_argument("--native", action="store_true",
                   help="capture via ScreenCaptureKit at true backing scale "
                        "(2x on a Retina display); non-deprecated engine")
    s.add_argument("--live", action="store_true",
                   help="wait until the app is actually RENDERING before capturing, "
                        "re-activating it as needed (polls the app's ui_age). A Unity "
                        "app that is not rendering still screenshots -- it returns its "
                        "last frame, which for Qud is the playfield with no UI overlay, "
                        "so a status-screen shot comes back looking like the map. "
                        "Exits 1 if it cannot settle, having still written the file.")
    s.add_argument("--live-age", type=float, default=None, metavar="N",
                   help="ui_age that counts as rendering (default 2)")
    s.add_argument("--live-timeout", type=float, default=None, metavar="S",
                   help="how long to keep retrying the activate (default 30)")
    s.set_defaults(fn=_cmd_shot)

    s = sub.add_parser("text")
    s.add_argument("target")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=_cmd_text)

    s = sub.add_parser("key")
    s.add_argument("target")
    s.add_argument("keys")
    s.add_argument("--focus", action="store_true",
                   help="activate + HID-tap delivery (for Unity/games that ignore background keys)")
    s.set_defaults(fn=_cmd_key)

    s = sub.add_parser("mouse", help="move the mouse to window-relative x y — no click (hover-state capture)")
    s.add_argument("target")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.set_defaults(fn=_cmd_mouse)

    s = sub.add_parser("click", help="click at window-relative x y (points)")
    s.add_argument("target")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.add_argument("--right", action="store_true", help="right-click")
    s.add_argument("--middle", action="store_true",
                   help="middle-click (Qud's Map Editor hangs commands off it)")
    s.add_argument("--double", action="store_true", help="double-click")
    s.add_argument("--hover", action="store_true",
                   help="post a real mouseMoved first (needed for Qud's legacy popups)")
    s.add_argument("--mod", default=None, metavar="ctrl+alt+shift",
                   help="modifiers HELD across the click (Qud's Map Editor: "
                        "ctrl=paint from palette, alt=sample to palette)")
    s.set_defaults(fn=_cmd_click)

    s = sub.add_parser("drag", help="press, move, release (selection rectangles)")
    s.add_argument("target")
    for _a in ("x1", "y1", "x2", "y2"):
        s.add_argument(_a, type=int)
    s.add_argument("--right", action="store_true", help="drag with the right button")
    s.add_argument("--middle", action="store_true", help="drag with the middle button")
    s.add_argument("--steps", type=int, default=12, help="intermediate moves (default 12)")
    s.add_argument("--mod", default=None, metavar="ctrl+alt+shift",
                   help="modifiers held for the whole gesture")
    s.add_argument("--hold", type=float, default=0.08,
                   help="seconds to hold the button before moving (default 0.08). "
                        "MEASURED: longer is WORSE — 0.5s stops Qud registering the "
                        "gesture as a drag at all. Lower it, do not raise it blindly.")
    s.set_defaults(fn=_cmd_drag)

    s = sub.add_parser("activate")
    s.add_argument("target")
    s.set_defaults(fn=_cmd_activate)

    s = sub.add_parser("inspect")
    s.add_argument("target")
    s.add_argument("depth", nargs="?", type=int, default=3)
    s.set_defaults(fn=_cmd_inspect)

    s = sub.add_parser("move", help="reposition a window to a zone or x y w h")
    s.add_argument("target")
    s.add_argument("rect", nargs="+",
                   help="zone name (e.g. top-right) or four ints: x y w h")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--topmost", dest="topmost", action="store_true", default=None,
                   help="pin the window above non-topmost windows")
    g.add_argument("--no-topmost", dest="topmost", action="store_false",
                   help="clear the window's topmost bit")
    s.set_defaults(fn=_cmd_move)

    s = sub.add_parser("stack", help="stack one window directly above another (same column)")
    s.add_argument("top", help="window to place on top (title substring)")
    s.add_argument("bottom", help="anchor window it sits above (title substring)")
    s.add_argument("--gap", type=int, default=8, help="pixels between them (default 8)")
    s.set_defaults(fn=_cmd_stack)

    s = sub.add_parser("dock", help="apply a window's standing dock rule (see docks.py)")
    s.add_argument("target", help="window id or title substring")
    s.set_defaults(fn=_cmd_dock)

    s = sub.add_parser("state", help="live game-state per app (the state tree evaluator, one line each)")
    s.add_argument("--ocr", action="store_true", help="also OCR the windows (refines menu screens)")
    s.set_defaults(fn=_cmd_state)

    s = sub.add_parser("assert", help="TDD assert: wait until an app reaches a state (exit 0) or time out (exit 1)")
    s.add_argument("--app", required=True, help="app id from the game tree (qud | raves)")
    s.add_argument("--node", help="tree node the app must be at (or inside), e.g. in_game")
    s.add_argument("--scene", help="exact self-reported scene, e.g. chargen_genotype")
    s.add_argument("--popup", nargs="?", const="", default=None,
                   help="a popup must be up (optionally of this type, e.g. message | yesno | input)")
    s.add_argument("--present", choices=["yes", "no"], help="window present / absent")
    s.add_argument("--ocr-contains", dest="ocr_contains", help="window OCR must contain this text")
    s.add_argument("--timeout", type=float, default=10.0)
    s.set_defaults(fn=_cmd_assert)

    s = sub.add_parser("goto", help="drive an app to a state-tree node along a planned route, e.g. hv goto qud in_game")
    s.add_argument("app", help="qud | raves")
    s.add_argument("node", help="tree node id, e.g. title | in_game")
    s.add_argument("--no-restart", action="store_true",
                   help="fail rather than reach the node by RESTARTING the app. A restart "
                        "discards unsaved progress (the save file is untouched); it becomes "
                        "reachable when an edge refuses, e.g. Qud's quit on a Classic save.")
    s.set_defaults(fn=_cmd_goto)

    s = sub.add_parser("plan", help="show the route `hv goto` would take, WITHOUT driving anything")
    s.add_argument("app", help="qud | raves")
    s.add_argument("node", nargs="?", default=None,
                   help="tree node id, e.g. title | in_game. OMIT it to list EVERY "
                        "reachable state and its cost")
    s.add_argument("--from", dest="frm", default=None,
                   help="plan from this state instead of the detected one (a node id, "
                        "'off' or 'unknown')")
    s.set_defaults(fn=_cmd_plan)

    s = sub.add_parser("scroll", help="wheel event at a window point (dy in LINES, + = up), e.g. hv scroll raves 960 540 --dy 1 --mod ctrl")
    s.add_argument("target")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.add_argument("--dy", type=int, default=1, help="lines; positive = wheel up/away")
    s.add_argument("--dx", type=int, default=0, help="lines; horizontal")
    s.add_argument("--mod", default=None, metavar="ctrl+alt+shift",
                   help="modifiers HELD across the wheel (not just flagged — see backend.py)")
    s.set_defaults(fn=_cmd_scroll)

    s = sub.add_parser("wish", help="run a Caves of Qud wish via the Raves bridge, e.g. hv wish godmode")
    s.add_argument("text", nargs="+", help="the wish text (godmode | item:<Blueprint> | xp:<n> | ...)")
    s.set_defaults(fn=_cmd_wish)

    sub.add_parser("saves", help="Qud's save list + picker row order, from DISK (no game launch)").set_defaults(fn=_cmd_saves)

    s = sub.add_parser("loadsave", help="load a NAMED Qud save (restarts to title if needed), e.g. hv loadsave meta")
    s.add_argument("name", nargs="+", help="the save's character name, exactly as the picker shows it")
    s.set_defaults(fn=_cmd_loadsave)

    s = sub.add_parser("back", help="close/back out of Qud's current modern menu (first-party uiback)")
    s.set_defaults(fn=lambda a: _print_json(_call({"op": P.OP_QUDBACK})))
    s = sub.add_parser("quit", help="stop an app and leave it stopped (TERM; --force for KILL)")
    s.add_argument("app", help="qud | raves")
    s.add_argument("--force", action="store_true",
                   help="SIGKILL instead of a graceful TERM — skips the app's own shutdown, "
                        "so it can lose state the app writes on the way out")
    s.set_defaults(fn=_cmd_quit)

    s = sub.add_parser("restart", help="clean restart: kill ALL instances, launch solo, wait for the window")
    s.add_argument("app", help="qud | raves")
    s.set_defaults(fn=_cmd_restart)

    s = sub.add_parser("trace", help="last N goto runs: what each STEERED BY and what it reached")
    s.add_argument("limit", nargs="?", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_trace)

    s = sub.add_parser("test", help="run a REGISTERED check from the tree by id (no args = list them)")
    s.add_argument("test", nargs="?", default=None)
    s.add_argument("--node", default=None, help="the node it is registered on (omit for harness-wide)")
    s.set_defaults(fn=_cmd_test)

    sub.add_parser("grant-input", help="raise the Accessibility prompt for the daemon "
                   "(input needs a DIFFERENT grant than capture)").set_defaults(fn=_cmd_grant_input)

    sub.add_parser("abort", help="PANIC: release focus/mouse now; refuse control ops for 30s").set_defaults(fn=_cmd_abort)

    sub.add_parser("install-daemon", help="launchd KeepAlive agent: the daemon restarts itself on crash").set_defaults(fn=_cmd_install_daemon)

    s = sub.add_parser("probe", help="is an app up, and in what state? (e.g. hv probe --app qud)")
    s.add_argument("--app", help="known app profile (see apps.py): qud")
    s.add_argument("--window", help="window title substring (if not using --app)")
    s.add_argument("--port", type=int, default=None, help="state-indicating localhost port")
    s.set_defaults(fn=_cmd_probe)

    s = sub.add_parser("text-diff", help="OCR two windows + diff their text (wrong labels/wording)")
    s.add_argument("a", help="reference window (e.g. CavesOfQud)")
    s.add_argument("b", help="current window (e.g. 'Raves of Qud')")
    s.add_argument("--threshold", type=float, default=0.82, help="fuzzy line-match ratio 0..1")
    s.set_defaults(fn=_cmd_text_diff)

    s = sub.add_parser("scene", help="drive to a named UI state + regression-diff vs its golden")
    s.add_argument("name", nargs="?", help="scene name (omit when using --all)")
    s.add_argument("--all", action="store_true", help="run every scene in the config")
    s.add_argument("--config", default="scenes.json", help="scenes JSON (default ./scenes.json)")
    s.add_argument("--bless", action="store_true", help="write the capture AS the golden reference")
    s.add_argument("--parity", action="store_true",
                   help="also drive the scene's `reference` window (e.g. Qud) and diff the two live")
    s.add_argument("--text", action="store_true",
                   help="with --parity: also OCR both windows + diff their text (label/wording gaps)")
    s.add_argument("--out", default=None, help="dir for captures + diffs (default <config-dir>/_regress)")
    s.add_argument("--settle", type=float, default=1.2, help="seconds after steps before capturing")
    s.set_defaults(fn=_cmd_scene)

    sub.add_parser("screen").set_defaults(fn=_cmd_screen)

    s = sub.add_parser("diff", help="score two captures + write a heatmap")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--out", default=None, help="write amplified diff heatmap here")
    s.add_argument("--crop-top", type=int, default=58, dest="crop_top",
                   help="px of OS chrome to skip for the content score")
    s.add_argument("--regions", action="store_true",
                   help="also localize divergence into a ranked cell punch-list + annotated image")
    s.add_argument("--grid", default="6x6", help="region grid for --regions (WxH cells)")
    s.add_argument("--threshold", type=float, default=92.0,
                   help="cells below this match %% are reported by --regions")
    s.add_argument("--regions-out", default=None, dest="regions_out",
                   help="write the annotated region image here")
    s.set_defaults(fn=_cmd_diff)

    s = sub.add_parser("zones", help="detect saturated colour rectangles")
    s.add_argument("img")
    s.add_argument("--top", type=int, default=0, help="px of OS chrome to skip")
    s.set_defaults(fn=_cmd_zones)

    s = sub.add_parser("responsive",
                       help="deterministic responsive-parity test: golem vs source")
    s.add_argument("golem", help="window ref for the generated/candidate window")
    s.add_argument("source", help="window ref for the reference window")
    s.add_argument("--threshold", type=float, default=99.0,
                   help="min content-match %% to pass a frame (default 99.0)")
    s.add_argument("--out-dir", default=None, dest="out_dir",
                   help="where to write per-frame captures + heatmaps")
    s.add_argument("--display", default="auto",
                   help="which monitor to sweep on: 'auto' (the display the golem "
                        "window is on — no need to make it the OS main display), "
                        "'main', or a display index (0,1,...). Default: auto")
    s.set_defaults(fn=_cmd_responsive)

    sub.add_parser("layouts", help="list known window layouts").set_defaults(fn=_cmd_layouts)

    s = sub.add_parser("layout", help="apply a named window layout")
    s.add_argument("name")
    s.set_defaults(fn=_cmd_layout)

    s = sub.add_parser("layout-save", help="snapshot the current arrangement as a layout")
    s.add_argument("name")
    s.add_argument("description", nargs="*")
    s.set_defaults(fn=_cmd_layout_save)

    s = sub.add_parser("launch", help="start a program by launcher name or raw spec")
    s.add_argument("name", help="a saved launcher name, or an OS spec (steam://…, /path.app, App Name)")
    s.set_defaults(fn=_cmd_launch)

    sub.add_parser("launchers", help="list saved launchers").set_defaults(fn=_cmd_launchers)

    s = sub.add_parser("launch-save", help="save a named launcher (name -> spec)")
    s.add_argument("name")
    s.add_argument("spec")
    s.set_defaults(fn=_cmd_launch_save)

    s = sub.add_parser("ocr", help="recognize text in a window (Vision) — read AX-opaque apps")
    s.add_argument("target")
    s.add_argument("--boxes", action="store_true", help="include bounding boxes as JSON")
    s.set_defaults(fn=_cmd_ocr)

    sub.add_parser("peers", help="list discovered bridge peers").set_defaults(fn=_cmd_peers)

    s = sub.add_parser("parity",
                       help="capture two windows (local or --peer) and screenshot-diff them")
    s.add_argument("a", help="window ref for side A")
    s.add_argument("b", help="window ref for side B")
    s.add_argument("--peer-a", dest="peer_a", default=None,
                   help="capture A from this bridge peer instead of locally")
    s.add_argument("--peer-b", dest="peer_b", default=None,
                   help="capture B from this bridge peer instead of locally")
    s.add_argument("--out", default=None, help="write the diff heatmap here")
    s.add_argument("--crop-top", type=int, default=58, dest="crop_top",
                   help="px of chrome to skip for the content score")
    s.add_argument("--size", default=None,
                   help="resize both local sides to WxH before capturing (e.g. 1920x1080)")
    s.add_argument("--settle", type=float, default=0.4,
                   help="seconds to wait after resizing for repaint (default 0.4)")
    s.set_defaults(fn=_cmd_parity)

    s = sub.add_parser("parity-sweep",
                       help="resize two windows through several sizes together, diffing each")
    s.add_argument("a", help="window ref for side A (e.g. 'Raves of Qud')")
    s.add_argument("b", help="window ref for side B (e.g. 'CavesOfQud')")
    s.add_argument("--sizes", default="1920x1080,1280x720,2560x1080,1000x1000,1080x1350",
                   help="comma list of WxH to sweep (default covers 16:9/wide/square/portrait)")
    s.add_argument("--peer-a", dest="peer_a", default=None, help="capture A from this bridge peer")
    s.add_argument("--peer-b", dest="peer_b", default=None, help="capture B from this bridge peer")
    s.add_argument("--out", default=None, help="output dir for captures + sweep.png")
    s.add_argument("--crop-top", type=int, default=58, dest="crop_top",
                   help="px of chrome to skip for the content score")
    s.add_argument("--settle", type=float, default=1.2,
                   help="seconds to wait after each resize (default 1.2)")
    s.add_argument("--height", type=int, default=520, help="per-row height in the sheet")
    s.add_argument("--regions", action="store_true",
                   help="also emit a per-size divergence punch-list + annotated image")
    s.add_argument("--no-restore", action="store_true", dest="no_restore",
                   help="leave windows at the last size instead of restoring originals")
    s.set_defaults(fn=_cmd_parity_sweep)

    s = sub.add_parser("tunnel",
                       help="SSH-tunnel a remote highvisor to this machine (encrypted)")
    s.add_argument("host", help="ssh host or user@host (remote needs sshd + key auth)")
    s.add_argument("--user", default=None, help="ssh user (if not in the host)")
    s.add_argument("--control", type=int, default=48730,
                   help="local port for the remote control daemon (default 48730)")
    s.add_argument("--web", type=int, default=48731,
                   help="local port for the remote cockpit (default 48731)")
    s.add_argument("--bridge", action="store_true", help="also forward the bridge port")
    s.add_argument("--bridge-port", type=int, default=48732, dest="bridge_port")
    s.add_argument("--print", dest="print_only", action="store_true",
                   help="show the ssh command instead of running it")
    s.set_defaults(fn=_cmd_tunnel)

    s = sub.add_parser("raw")
    s.add_argument("json")
    s.set_defaults(fn=_cmd_raw)

    return p


def main(argv=None):
    # Window titles are arbitrary Unicode; the Windows console is often cp1252.
    # Reconfigure to UTF-8 with replacement so printing never crashes.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    global _HOST, _PORT
    _HOST, _PORT = args.host, args.port
    try:
        return args.fn(args) or 0
    except ConnectionRefusedError:
        print("cannot reach daemon on %s:%d — is it running? (python -m highvisor.server)"
              % (args.host, args.port), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
