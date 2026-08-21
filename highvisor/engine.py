"""Engine — the single-threaded action queue that owns the backend.

Every op runs on ONE worker thread. That thread calls ``backend.thread_init()``
first and then processes jobs serially, so all UIA/COM calls live in one apartment
and no backend method ever runs concurrently with another. Server connection
threads hand work in via :meth:`submit` and block until the worker replies.

Why single-threaded: Windows UI Automation is COM, and COM objects are apartment
bound. Serializing on one thread is simpler and safer than marshalling across
threads, and desktop automation is not throughput-bound anyway.
"""
import base64
import queue
import threading
import traceback

from . import protocol as P
from .backend import BackendError

__version__ = "0.0.1"


def _png_dims(png: bytes):
    """(width, height) from a PNG's IHDR, or None. Cheap header read — avoids a
    full decode just to report the capture's pixel size."""
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR":
        return (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big"))
    return None


def _slim_state(st):
    """A gamestate entry reduced to what an assert/goto caller needs to read."""
    if not st:
        return None
    return {"node": st.get("node"), "label": st.get("label"), "off": st.get("off"),
            "path": st.get("path"), "via": st.get("via"),
            "scene": (st.get("signals") or {}).get("scene"),
            "extra": st.get("extra")}


def _step_ok(ok, detail=None, error=None):
    """A step result that carries `error` ONLY when it failed.

    Trivial, and worth a name: the obvious `{"ok": r.ok, "error": r.error or "key failed"}`
    stamps a fallback message onto SUCCESSES too, so a passing route prints a column of
    "activate failed / key failed" next to ok=True. Read six months from now that is a
    bug report about a working feature.
    """
    out = {"ok": bool(ok)}
    if detail:
        out["detail"] = detail
    if not ok and error:
        out["error"] = error
    return out


def _ocr_find(boxes, want):
    """Find the OCR line for a UI label. Space-insensitive: Vision splits tight
    monospace ('Options' -> 'Opti ons' on Raves' Source Code Pro), so compare with
    all whitespace stripped. Exact (normalized) match beats substring; among
    substrings the shortest line wins (long lines are prose, not buttons)."""
    wn = "".join(str(want).lower().split())
    norm = lambda t: "".join(t.lower().split())
    exact = [x for x in boxes if norm(x["text"]) == wn]
    subs = sorted((x for x in boxes if wn in norm(x["text"])),
                  key=lambda x: len(x["text"]))
    return (exact or subs or [None])[0]


class _Job:
    __slots__ = ("request", "event", "result")

    def __init__(self, request):
        self.request = request
        self.event = threading.Event()
        self.result = None


class Engine:
    def __init__(self, backend, bus=None):
        self.backend = backend
        self.bus = bus  # optional EventBus: each op is published for the onscreen log
        from .guard import ControlGuard
        self.guard = ControlGuard(bus)   # the timeshare guard (focus/mouse save-restore + abort)
        self.bridge = None  # optional Bridge: set by the server for peer_* ops
        self._q: "queue.Queue[_Job]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="hv-engine",
                                        daemon=True)
        self._started = threading.Event()
        self._init_error = None

    def start(self):
        self._thread.start()
        self._started.wait()
        if self._init_error is not None:
            raise RuntimeError("backend init failed: %s" % self._init_error)

    def submit(self, request: dict) -> dict:
        """Enqueue a request and block until the worker returns its response."""
        job = _Job(request)
        self._q.put(job)
        job.event.wait()
        return job.result

    # --------------------------------------------------------------- worker
    def _run(self):
        try:
            self.backend.thread_init()
        except Exception as e:  # backend unusable — surface via start()
            self._init_error = "%s\n%s" % (e, traceback.format_exc())
            self._started.set()
            return
        self._started.set()
        while True:
            job = self._q.get()
            if job.request is None:  # shutdown sentinel
                job.event.set()
                return
            try:
                job.result = self._dispatch(job.request)
            except BackendError as e:
                job.result = {"ok": False, "error": str(e)}
            except Exception as e:
                job.result = {"ok": False,
                              "error": "%s: %s" % (type(e).__name__, e)}
            finally:
                job.event.set()
            if self.bus is not None:
                self._publish_op(job.request, job.result)

    def _publish_op(self, req: dict, res: dict) -> None:
        """Emit a compact event for the onscreen log — never the screenshot bytes."""
        op = req.get("op")
        # polled constantly / internal reads (the orchestrator watches via these) —
        # would drown the log, so keep them out of the onscreen stream.
        if op in (P.OP_PING, P.OP_LIST, P.OP_INSPECT, P.OP_OCR):
            return
        f = {"op": op, "ok": bool(res.get("ok"))}
        if req.get("target"):
            f["target"] = req["target"]
        if res.get("tier") is not None:
            f["tier"] = res["tier"]
        if res.get("detail"):
            f["detail"] = res["detail"]
        if op == P.OP_SHOT and res.get("ok"):
            f["detail"] = "%d bytes" % res.get("bytes", 0)
        if res.get("error"):
            f["error"] = res["error"]
        try:
            self.bus.publish("op", **f)
        except Exception:
            pass

    def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        b = self.backend

        if op == P.OP_PING:
            resp = {"ok": True, "backend": b.name, "version": __version__}
            try:                       # windows: DPI awareness the daemon runs under
                from .backends import windows as _w
                resp["dpi_status"] = _w.DPI_STATUS
            except Exception:
                pass
            return resp

        if op == P.OP_LIST:
            from .apps import classify_target
            rows = []
            for t in b.list_targets():
                d = t.to_dict()
                d["role"] = classify_target(d.get("title"), d.get("class_name"),
                                            d.get("path", ""))
                rows.append(d)
            return {"ok": True, "targets": rows}

        if op == P.OP_SHOT:
            settled = None
            if req.get("live"):
                settled = self._settle_rendering(
                    b, req.get("target"),
                    max_age=float(req.get("live_age") or 2),
                    timeout=float(req.get("live_timeout") or 30))
            png = b.screenshot(req.get("target"), native=bool(req.get("native")))
            resp = {"ok": True, "bytes": len(png),
                    "png_b64": base64.b64encode(png).decode("ascii")}
            dims = _png_dims(png)
            if dims:
                resp["w"], resp["h"] = dims
            if settled is not None:
                resp.update(settled)
            return resp

        # For actions, the response IS the ActionResult dict: its ``ok`` reports
        # whether the action landed (RPC-level failures come back as exceptions).
        # Focus/mouse-stealing ops go through the TIMESHARE GUARD (audio countdown,
        # focus+mouse save/restore, abort channels, 20s cap — see guard.py).
        if op in (P.OP_ACTIVATE, P.OP_TEXT, P.OP_KEY, P.OP_CLICK, P.OP_MOUSE, P.OP_SCROLL, P.OP_DRAG):
            _gerr = self.guard.begin()
            if _gerr:
                return {"ok": False, "error": _gerr}
        if op == P.OP_ACTIVATE:
            return b.activate(req["target"]).to_dict()

        if op == P.OP_TEXT:
            return b.text(req["target"], req.get("text", "")).to_dict()

        if op == P.OP_KEY:
            return b.key(req["target"], req.get("keys", ""),
                         focus=bool(req.get("focus", False))).to_dict()

        if op == P.OP_CLICK:
            kw = {"button": req.get("button", "left"),
                  "double": bool(req.get("double", False))}
            if req.get("hover"):   # only forward when asked — backends without the arg won't see it
                kw["hover"] = True
            if req.get("modifiers"):   # same rule: absent unless asked
                kw["modifiers"] = str(req["modifiers"])
            return b.click(req["target"], int(req.get("x", 0)), int(req.get("y", 0)),
                           **kw).to_dict()

        if op == P.OP_DRAG:
            kw = {"button": req.get("button", "left"),
                  "steps": int(req.get("steps", 12))}
            if req.get("hold") is not None:
                kw["hold"] = float(req["hold"])
            if req.get("modifiers"):
                kw["modifiers"] = str(req["modifiers"])
            return b.drag(req["target"], int(req.get("x1", 0)), int(req.get("y1", 0)),
                          int(req.get("x2", 0)), int(req.get("y2", 0)), **kw).to_dict()

        if op == P.OP_SCROLL:
            # A WHEEL event, optionally modified. Distinct from click because a wheel carries
            # no position: it lands under the OS cursor, so the backend warps first. Guarded
            # like every other input op — it moves the mouse and takes focus.
            return b.scroll(req["target"], int(req.get("x", 0)), int(req.get("y", 0)),
                            dy=int(req.get("dy", 1)), dx=int(req.get("dx", 0)),
                            modifiers=str(req.get("modifiers", ""))).to_dict()

        if op == P.OP_MOUSE:
            # pure hover: warp + a real mouseMoved so engines that read
            # Input.mousePosition (Unity) see it — no button events. THE tool for
            # capturing hover/highlight states without changing app state.
            return b.mouse_move(req["target"], int(req.get("x", 0)),
                                int(req.get("y", 0))).to_dict()

        if op == P.OP_INSPECT:
            tree = b.inspect(req["target"], int(req.get("depth", 3)))
            return {"ok": True, "tree": tree.to_dict()}

        if op == P.OP_OCR:
            res = b.ocr(req["target"])
            res["ok"] = True
            res["text"] = "\n".join(x["text"] for x in res.get("boxes", []))
            return res

        if op == P.OP_SCREEN:
            w, h = b.screen_size()
            return {"ok": True, "w": w, "h": h}

        if op == P.OP_DISPLAYS:
            return {"ok": True, "displays": b.displays()}

        if op == P.OP_MOVE:
            topmost = req.get("topmost")  # tri-state: True/False/None
            zone = req.get("zone")
            if zone:  # resolve the named zone against the physical display size
                from .backend import zone_rect
                sw, sh = b.screen_size()
                x, y, w, h = zone_rect(zone, sw, sh)
            else:
                x, y, w, h = (int(req["x"]), int(req["y"]),
                              int(req["w"]), int(req["h"]))
            tgt = self._find_win(b.list_targets(), req["target"])
            return self._place(b, (tgt.id if tgt else req["target"]),
                               (tgt.title if tgt else str(req["target"])),
                               x, y, w, h, topmost).to_dict()

        if op == P.OP_STACK:
            return self._stack_above(b, req.get("top"), req.get("bottom"),
                                     int(req.get("gap", 8)))

        if op == P.OP_DOCK:
            return self._dock(b, req.get("target"))

        if op == P.OP_PROBE:
            return self._probe(b, req.get("app"), req.get("window"), req.get("port"))

        if op == P.OP_GAMETREE:
            from . import gametree
            return {"ok": True, "tree": gametree.load_tree(force=bool(req.get("reload")))}

        if op == P.OP_GAMESTATE:
            return self._gamestate(b, ocr=bool(req.get("ocr", False)))

        if op == P.OP_GAMEGO:
            return self._gamego(b, req.get("app"), req.get("node"),
                                no_restart=bool(req.get("no_restart")))

        if op == P.OP_PLAN:
            return self._plan_route(b, req.get("app"), req.get("node"), req.get("from"))

        if op == P.OP_ASSERT:
            return self._assert_state(b, req)

        if op == P.OP_WRITE_TEXT:
            return self._write_text(req.get("path"), req.get("content", ""))

        if op == P.OP_QUDWISH:
            return self._qudwish(req.get("wish", ""))

        if op == P.OP_QUDBACK:
            return self._qud_bridge("uiback")

        if op == P.OP_QUDBRIDGE:
            # Generic first-party passthrough. Every mod command so far got its own op
            # (qudwish, qudback, loadsave...), which is fine for the handful the cockpit
            # calls by name and useless for exercising a NEW one: `pick` was deployed to
            # the mod and could not be invoked from the CLI at all, because reaching it
            # meant either adding a fourth bespoke op or authoring a gametree edge around
            # an unproven command. This is the "try it once" path those both lacked.
            # focus defaults ON (see _qud_bridge) -- callers opt OUT to reproduce what a
            # command does with Qud in the background, which is Raves' normal case.
            return self._qud_bridge(str(req.get("name", "")), args=req.get("args"),
                                    focus=bool(req.get("focus", True)))

        if op == P.OP_QUD_SAVES:
            return self._qud_saves()

        if op == P.OP_LOAD_SAVE:
            return self._load_save(b, req.get("name", ""))

        if op == P.OP_TRACE:
            return self._read_trace(req.get("limit", 20))

        if op == P.OP_QUIT:
            return self._quit_app(b, req.get("app"), force=bool(req.get("force")))

        if op == P.OP_RESTART:
            return self._restart_app(b, req.get("app", ""))

        if op == P.OP_RUN_TEST:
            return self._run_test(req.get("node") or "", req.get("test") or "")

        if op == P.OP_GRANT_INPUT:
            return b.request_input_grant()

        if op == P.OP_ABORT:
            return self.guard.abort("op")

        if op == P.OP_LAYOUT_LIST:
            from .layouts import load_layouts
            return {"ok": True, "layouts": [
                {"name": n, "description": l.get("description", ""),
                 "placements": len(l.get("placements", []))}
                for n, l in load_layouts().items()]}

        if op == P.OP_LAYOUT_APPLY:
            return self._apply_layout(b, req.get("name"))

        if op == P.OP_PEERS:
            if self.bridge is None:
                return {"ok": False, "error": "bridge not running"}
            return {"ok": True, "peers": self.bridge.peers(),
                    "self": self.bridge.identity()}

        if op == P.OP_PEER_SHOT:
            if self.bridge is None:
                return {"ok": False, "error": "bridge not running"}
            return self.bridge.request_shot(req.get("peer"), req.get("target"))

        if op == P.OP_LAUNCH:
            from .launch import resolve_launch
            try:
                spec, largs = resolve_launch(req.get("name", ""))
            except KeyError as e:
                return {"ok": False, "error": str(e.args[0] if e.args else e)}
            if not spec:
                return {"ok": False, "error": "no launcher/spec %r" % req.get("name")}
            before = {t.id for t in b.list_targets()}
            d = b.launch(spec, largs).to_dict()
            d["spec"] = spec
            # defacto: if the just-launched window carries a standing dock rule
            # (e.g. Raves -> above Caves of Qud), highvisor stacks it on its own.
            dock = self._autodock_new(b, before)
            if dock is not None:
                d["dock"] = dock
            return d

        if op == P.OP_LAUNCH_LIST:
            from .launch import load_launchers
            return {"ok": True, "launchers": load_launchers()}

        if op == P.OP_LAUNCH_SAVE:
            from .launch import save_launcher
            path = save_launcher(req["name"], req["spec"])
            return {"ok": True, "saved": req["name"], "spec": req["spec"], "path": path}

        if op == P.OP_LAYOUT_SAVE:
            from .layouts import save_layout
            placements = []
            for t in b.list_targets():
                label = t.title or t.class_name
                if not label:
                    continue  # skip the untitled desktop/wallpaper layer
                # Absolute rects: an exact freeze of the current arrangement, which
                # round-trips faithfully across displays (incl. negative-origin
                # secondary monitors). Hand-authored layouts use zone/frac instead.
                placements.append({"match": label,
                                   "rect": [t.x, t.y, t.w, t.h]})
            path = save_layout(req["name"], {
                "description": req.get("description", "saved arrangement"),
                "placements": placements})
            return {"ok": True, "saved": req["name"], "path": path,
                    "windows": len(placements),
                    "detail": "%d windows -> %s" % (len(placements), req["name"])}

        return {"ok": False, "error": "unknown op: %r" % op}

    def _find_win(self, wins, label):
        """First window whose title/owner contains ``label`` (case-insensitive)."""
        m = (label or "").lower()
        for t in wins:
            if m and (m in (t.title or "").lower() or m in (t.class_name or "").lower()):
                return t
        return None

    def _all_wins(self, wins, label):
        """EVERY window matching ``label`` — how duplicate instances become visible."""
        m = (label or "").lower()
        if not m:
            return []
        return [t for t in wins
                if m in (t.title or "").lower() or m in (t.class_name or "").lower()]

    def _stack_above(self, b, top_label, bottom_label, gap=8):
        """Move ``top`` into the anchor's column (matched x + width), directly above it."""
        if not top_label or not bottom_label:
            return {"ok": False, "error": "stack needs top and bottom"}
        wins = b.list_targets()
        top = self._find_win(wins, top_label)
        bot = self._find_win(wins, bottom_label)
        if bot is None:
            return {"ok": False, "error": "anchor %r not found" % bottom_label}
        if top is None:
            return {"ok": False, "error": "%r not found" % top_label}
        x, w, h = bot.x, bot.w, bot.h            # same column + size as the anchor
        y = bot.y - gap - h                      # stacked directly above it
        r = self._place(b, top.id, top.title, int(x), int(y), int(w), int(h), None)
        return {"ok": r.ok, "top": top.id, "bottom": bot.id,
                "rect": [int(x), int(y), int(w), int(h)], "error": r.error}

    # ---------------------------------------------------------------- placement

    SELF_PLACING = ("raves of qud",)

    def _place(self, b, win_id, title, x, y, w, h, topmost=None):
        """Move ONE window, by whichever channel that window actually obeys.

        Godot's borderless window cannot be moved through the accessibility layer -- AX sets
        either fail or land at wild coordinates -- so Raves is asked to place ITSELF: highvisor
        writes window_rect.json and Settings.gd applies it with DisplayServer within ~0.5s (the
        reverse direction of its state-report contract).

        This lives in ONE place because it was in exactly one CALLER before, `stack`, and every
        other route into a move -- `hv move`, `hv dock`, and every layout -- went straight to AX
        and silently did nothing to Raves. `hv layout loop` reported "move did not land" and
        placed 1 of 5, which reads as a broken layout rather than a window that needs a different
        channel; and I had been working around it by passing explicit coordinates, which is how
        Raves kept ending up on the laptop screen instead of the external monitor.
        """
        if any(k in (title or "").lower() for k in Engine.SELF_PLACING):
            r2 = self._move_raves_file(b, win_id, int(x), int(y), int(w), int(h))
            if r2.get("ok"):
                class _R:  # the same shape b.move() returns, so callers need no special case
                    ok, error, detail = True, None, "placed via window_rect.json"
                    def to_dict(self_):
                        return {"ok": True, "detail": _R.detail,
                                "rect": [int(x), int(y), int(w), int(h)], "via": "file"}
                return _R()
            # fall through to AX as a last resort -- it may be a differently-built window
        return b.move(win_id, int(x), int(y), int(w), int(h), topmost)

    def _move_raves_file(self, b, win_id, x, y, w, h, timeout_s=6.0):
        """Placement via Raves' window_rect.json poll (Settings.gd applies it with
        DisplayServer within ~0.5s). Verified by CG frame readback, ±3px."""
        import json as _json
        import os as _os
        import time as _time
        path = _os.path.expanduser(
            "~/Library/Application Support/RavesOfQud/window_rect.json")
        try:
            with open(path, "w") as f:
                _json.dump({"x": x, "y": y, "w": w, "h": h, "ts": _time.time()}, f)
        except OSError as e:
            return {"ok": False, "error": "window_rect write failed: %s" % e}
        end = _time.monotonic() + timeout_s
        while _time.monotonic() < end:
            _time.sleep(0.5)
            t = next((t for t in b.list_targets() if t.id == win_id), None)
            if t and all(abs(a - b_) <= 3 for a, b_ in
                         ((t.x, x), (t.y, y), (t.w, w), (t.h, h))):
                return {"ok": True}
        return {"ok": False, "error": "raves did not land on the rect (file channel)"}

    def _dock(self, b, target):
        """Apply the standing dock rule for ``target`` (id or title substring)."""
        wins = b.list_targets()
        win = next((t for t in wins if t.id == target), None) or self._find_win(wins, target)
        if win is None:
            return {"ok": False, "error": "no window %r" % target}
        from .docks import rule_for
        label = win.title or win.class_name or ""
        rule = rule_for(label)
        if not (rule and rule.get("above")):
            return {"ok": False, "error": "no dock rule for %r" % label}
        return self._stack_above(b, label, rule["above"], int(rule.get("gap", 8)))

    def _autodock_new(self, b, before_ids, deadline_s=5.0):
        """Poll briefly for a newly-appeared window with a dock rule; apply it once.
        Bounded — returns the dock result as soon as the new window shows, or None."""
        import time
        from .docks import rule_for
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            for t in b.list_targets():
                if t.id in before_ids:
                    continue
                label = t.title or t.class_name or ""
                rule = rule_for(label)
                if rule and rule.get("above"):
                    return self._stack_above(b, label, rule["above"], int(rule.get("gap", 8)))
            time.sleep(0.4)
        return None

    def _probe(self, b, app=None, window=None, port=None):
        """Report whether an app is up and in what state, from its window + a
        state-indicating localhost port. Takes an app profile name (see apps.py)
        or an explicit window label (+ optional port)."""
        prof = {}
        if app:
            from .apps import PROFILES
            prof = PROFILES.get(app)
            if prof is None:
                return {"ok": False, "error": "no app profile %r" % app}
            window = window or prof.get("window")
            if port is None:
                port = prof.get("port")
        win = self._find_win(b.list_targets(), window) if window else None
        port_open = False
        if port:
            import socket
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
                    port_open = True
            except OSError:
                port_open = False
        if win is None:
            state = prof.get("off_state", "off")
        elif port_open:
            state = prof.get("port_state", "up")
        else:
            state = prof.get("window_state", "up")
        return {"ok": True, "app": app, "running": win is not None, "state": state,
                "port": port, "port_open": port_open,
                "window": win.to_dict() if win else None}

    def _write_text(self, path, content):
        """Write a small text/JSON config file. Restricted to under $HOME so the cockpit's live
        tuning tools (e.g. the title-bg nudge) can write app config a game hot-reloads, without a
        general filesystem-write capability."""
        import os
        if not path:
            return {"ok": False, "error": "no path"}
        home = os.path.realpath(os.path.expanduser("~"))
        full = os.path.realpath(os.path.expanduser(str(path)))
        if full != home and not full.startswith(home + os.sep):
            return {"ok": False, "error": "path must be under $HOME"}
        try:
            d = os.path.dirname(full)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(str(content))
            return {"ok": True, "path": full, "bytes": len(str(content))}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------- qudwish (Qud bridge wish)
    def _qudwish(self, wish):
        """Execute a Caves of Qud wish (godmode, item:<blueprint>, xp:<n>, ...) through the
        Raves mod bridge (127.0.0.1:48710, same 4-byte-BE-length JSON framing as ours).
        The wish is chased with a "wait": the mod drains wishes on Qud's game thread,
        which sleeps while Qud is unfocused with no turn passing — the wait wakes the
        parked input loop so the wish applies immediately instead of pending silently.
        (Costs one game turn; the cockpit's use cases — godmode, test gear — don't care.)"""
        import json as _json
        import socket as _socket
        import struct as _struct
        wish = (wish or "").strip()
        if not wish:
            return {"ok": False, "error": "empty wish"}
        from .apps import PROFILES
        port = PROFILES.get("qud", {}).get("port", 48710)

        def _frame(obj):
            payload = _json.dumps(obj).encode("utf-8")
            return _struct.pack(">I", len(payload)) + payload

        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(_frame({"type": "command", "name": "wish", "wish": wish}))
                s.sendall(_frame({"type": "command", "name": "wait"}))
            return {"ok": True, "wish": wish}
        except OSError as e:
            return {"ok": False,
                    "error": "Qud bridge :%s unreachable (%s) — is Qud in-game?" % (port, e)}

    def _qud_command(self, command):
        """Push a named QUD command (CmdQuit, CmdEquipment, …) through the mod bridge.

        Distinct from _qud_bridge, which names a MOD command: this one names a command
        in Qud's own table, so it runs through Qud's command path and any UI it owns
        opens for real. Binding-independent -- no key guessing.
        """
        import json as _json
        import socket as _socket
        import struct as _struct
        from .apps import PROFILES
        port = PROFILES.get("qud", {}).get("port", 48710)
        payload = _json.dumps({"type": "command", "name": "command",
                               "command": command}).encode("utf-8")
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(_struct.pack(">I", len(payload)) + payload)
            return {"ok": True, "command": command}
        except OSError as e:
            return {"ok": False, "error": "qud bridge: %s" % e}

    def _qud_popup_answer(self, btn):
        """Answer Qud's OWN popup by its bottom-button command (Yes/No/Cancel).

        The mod dismisses the popup it announced, so this needs no keys and no focus --
        which matters because Qud's modern UI ignores OS-synthesized keys entirely.
        Answering when no popup is up is a harmless no-op (the mod logs "no target"),
        so a fixed sequence can be sent without first detecting each prompt.
        """
        import json as _json
        import socket as _socket
        import struct as _struct
        from .apps import PROFILES
        port = PROFILES.get("qud", {}).get("port", 48710)
        payload = _json.dumps({"type": "command", "name": "popup",
                               "action": "button", "btn": btn}).encode("utf-8")
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(_struct.pack(">I", len(payload)) + payload)
            return {"ok": True, "btn": btn}
        except OSError as e:
            return {"ok": False, "error": "qud bridge: %s" % e}

    def _qud_command_chain(self, command, answers, timeout=25.0):
        """Run a Qud command and answer the modals it raises AS THEY ARRIVE.

        This replaces a blind `send, sleep 1.2, answer, sleep 1.2, answer` chain, and the
        difference is not cosmetic. Both writes in the blind version succeeded whatever the
        game did -- a TCP write to the mod cannot fail just because no popup was up -- so
        every step reported ok and the only thing that could ever fail was the edge's verify,
        45 seconds later, with no record of which prompt went unanswered. Three wrong
        diagnoses came out of that gap ("stale save", "Raves contention", "process age");
        what it actually hid is below.

        **Qud's quit chain is not a fixed length.** Decompiled from the shipped assembly
        (XRL.Core.XRLCore, case "CmdQuit"): after "Are you sure you want to quit?" -> Yes and
        "Do you want to save first?", a THIRD prompt appears unless
        `Options.DisablePermadeath || CheckpointingSystem.IsCheckpointingEnabled()` -- and
        checkpointing is on only for **Wander/Roleplay** saves. On a **Classic** save Qud
        raises `Popup.AskString(… Type 'ABANDON' to confirm)`, a TEXT-INPUT modal. Two blind
        answers have nothing for it, so it sits there with the turn thread parked inside it,
        the state file reads In-Game forever, and -- the part that made this look like
        process ageing -- every later `goto in_game` is a NO-OP, because the tree still calls
        that state in_game, so "retrying on a fresh load" never reloaded anything.

        We answer that prompt by REFUSING it. Typing ABANDON is the quit-without-saving path
        for a permadeath character: it sets a DeathReason and ends the run. A harness must not
        destroy a character to satisfy a state transition, so the input modal is CANCELLED and
        the edge fails with what happened. The planner's `"*" -> title` restart edge is the
        non-destructive way out, and it will be re-planned onto automatically.

        Matching is by CONTENT, never by the popup's id. The mod re-publishes the live popup
        on every client connect, and highvisor's own state poller connects about twice a
        second, so one prompt arrives over and over with a fresh id each time -- answering per
        id sends the second answer to the first prompt (measured: it answered "Are you sure
        you want to quit?" twice in 0.31s).
        """
        import json as _json
        import socket as _socket
        import struct as _struct
        import time as _time
        from .apps import PROFILES
        port = PROFILES.get("qud", {}).get("port", 48710)

        def _send(sock, obj):
            p = _json.dumps(obj).encode("utf-8")
            sock.sendall(_struct.pack(">I", len(p)) + p)

        def _sig(f):
            return ((f.get("message") or "")
                    + "|" + ",".join(b.get("command", "") for b in f.get("buttons") or []))

        pending = list(answers or [])
        answered = []
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                # The mod re-broadcasts the live popup to a joining client, so a short drain
                # here says whether a modal was ALREADY up. That is not our prompt and must
                # not eat an answer -- record it as seen and let the edge's preflight own it.
                buf = b""
                seen = set()
                deadline = _time.time() + 0.6
                while _time.time() < deadline:
                    s.settimeout(max(0.05, deadline - _time.time()))
                    try:
                        chunk = s.recv(65536)
                    except (_socket.timeout, OSError):
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= 4:
                        n = _struct.unpack(">I", buf[:4])[0]
                        if len(buf) < 4 + n:
                            break
                        body, buf = buf[4:4 + n], buf[4 + n:]
                        try:
                            f = _json.loads(body.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if f.get("type") == "popup" and f.get("active"):
                            seen.add(_sig(f))

                _send(s, {"type": "command", "name": "command", "command": command})
                if not pending:
                    return {"ok": True, "command": command, "answered": []}

                # Keep reading until every answer has found its prompt, then a beat longer to
                # catch a prompt we did NOT expect -- which is the whole point.
                deadline = _time.time() + float(timeout)
                grace = None
                while _time.time() < deadline:
                    s.settimeout(0.3)
                    try:
                        chunk = s.recv(65536)
                    except _socket.timeout:
                        if grace is not None and _time.time() > grace:
                            return {"ok": True, "command": command, "answered": answered}
                        continue
                    except OSError as e:
                        return {"ok": False, "error": "qud bridge: %s" % e}
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= 4:
                        n = _struct.unpack(">I", buf[:4])[0]
                        if len(buf) < 4 + n:
                            break
                        body, buf = buf[4:4 + n], buf[4 + n:]
                        try:
                            f = _json.loads(body.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if f.get("type") != "popup" or not f.get("active"):
                            continue
                        sig = _sig(f)
                        if sig in seen:
                            continue          # a re-announce of one we already handled
                        seen.add(sig)
                        msg = (f.get("message") or "").strip()
                        if f.get("kind") == "input":
                            # The ABANDON prompt (or any other typed confirmation). Cancel it
                            # so the game is left live and intact, and say why we stopped.
                            _send(s, {"type": "command", "name": "popup",
                                      "action": "button", "btn": "Cancel"})
                            return {"ok": False, "answered": answered, "refused": True,
                                    "error": "%s raised a TEXT-INPUT confirmation this edge "
                                             "must not answer: %r. Qud asks that only when the "
                                             "save has no checkpointing (a Classic character), "
                                             "and typing it quits WITHOUT SAVING, ending a "
                                             "permadeath run. Cancelled it; the game is still "
                                             "live. Use a Wander/Roleplay save, or reach the "
                                             "title by restarting."
                                             % (command, msg[:90])}
                        if not pending:
                            _send(s, {"type": "command", "name": "popup",
                                      "action": "button", "btn": "Cancel"})
                            return {"ok": False, "answered": answered,
                                    "error": "%s raised an unexpected extra prompt %r after "
                                             "%d answer(s); cancelled it rather than guessing"
                                             % (command, msg[:90], len(answered))}
                        btn = pending.pop(0)
                        have = [b.get("command", "") for b in f.get("buttons") or []]
                        if btn not in have:
                            _send(s, {"type": "command", "name": "popup",
                                      "action": "button", "btn": "Cancel"})
                            return {"ok": False, "answered": answered,
                                    "error": "%s: prompt %r offers %s, not %r -- cancelled "
                                             "rather than pressing the wrong button"
                                             % (command, msg[:60], have, btn)}
                        _send(s, {"type": "command", "name": "popup",
                                  "action": "button", "btn": btn})
                        answered.append({"prompt": msg[:60], "btn": btn})
                        if not pending:
                            # Everything we planned for is answered. Watch a little longer:
                            # a further prompt now is the Classic-save case above.
                            grace = _time.time() + 3.0

                if pending:
                    return {"ok": False, "answered": answered,
                            "error": "%s: no popup appeared to answer %r (waited %.0fs). The "
                                     "command reached the mod, so the turn thread never got "
                                     "to it -- a modal already up, or no live game."
                                     % (command, pending[0], timeout)}
                return {"ok": True, "command": command, "answered": answered}
        except OSError as e:
            return {"ok": False, "error": "qud bridge: %s" % e}

    def _qud_bridge(self, name, focus=True, args=None):
        """Send a bare {"type":"command","name":...} frame to the Qud mod bridge
        (listener is up from the main menu on — ModSensitiveCacheInit). First-party
        UI driving: e.g. "uiback" fires the modern-UI CancelButton, the real Escape
        for menu screens that ignore every OS-synthesized key.

        ACTIVATES QUD FIRST, and that is load-bearing. These commands marshal onto the
        mod's uiQueue, and **Unity does not drain that queue while the window is in the
        background** (measured: a probe command logged nothing backgrounded and ran the
        instant Qud was focused). The frame is accepted, queued, and simply sits there —
        so an unfocused `uiback` looks like it worked and the screen stays up. That in
        turn strands the pair: Qud holds a status screen, stops publishing snapshots, and
        Raves can never leave its title screen. Diagnosed as "the Raves goto is broken"
        more than once; it is this.

        Anything that drives the app's UI has to go through here focused. Pass focus=False
        only for a command you know touches no Unity object."""
        import json as _json
        import socket as _socket
        import struct as _struct
        from .apps import PROFILES
        win = PROFILES.get("qud", {}).get("window", "CavesOfQud")
        if focus:
            # WAIT FOR THE QUEUE TO BE DRAINING, don't guess at it. This used to check
            # "is Qud frontmost" and, if not, activate and sleep a flat 2s. Both halves
            # were wrong in the same direction. `activate` frequently does not take
            # (three attempts in a row measured before one landed), and frontmost is not
            # the condition that matters -- a Qud that IS frontmost but has stopped
            # rendering drains nothing, and that case paid no settle at all because the
            # front check passed.
            #
            # The failure is silent by construction: a TCP write to the mod cannot fail
            # just because the queue is parked, so the command reports ok and simply
            # never happens. A FULL 2 sweep across the status tabs failed six of eight
            # this way, each tab reporting the PREVIOUS tab -- the statustab frames were
            # sitting in an undrained queue, applying one step late when the next
            # capture's activate happened to land.
            #
            # ui_age settles that directly: it is only low if the UI actually ran.
            try:
                self._settle_rendering(self.backend, win, max_age=2, timeout=20)
            except Exception:
                pass                # not fatal: a rendering Qud is the common case anyway
        port = PROFILES.get("qud", {}).get("port", 48710)
        frame = {"type": "command", "name": name}
        # Extra fields ride ALONGSIDE name — the mod reads its arguments off the same flat
        # command object (f.TryGetValue("tab", ...)), so a nested dict would be invisible to it.
        for k, v in (args or {}).items():
            if k not in ("type", "name"):
                frame[k] = v
        payload = _json.dumps(frame).encode("utf-8")
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(_struct.pack(">I", len(payload)) + payload)
                # HOLD THE SOCKET OPEN briefly. Closing the instant sendall returns races the
                # mod's per-client reader: the server logs "dropped slow/broken client: the
                # socket has been shut down" and the command is lost. The bridge client the
                # capture tools use keeps its socket alive, which is why an identical frame
                # sent from there worked while `hv back` silently did nothing.
                import time as _t2
                _t2.sleep(0.4)
            # `focused` echoes what we DID, not a measurement -- it was hardcoded True and so
            # reported a focused send for a --no-focus one, in the middle of an experiment
            # about focus. (Whether the activate actually took is `hv activate`'s business.)
            return {"ok": True, "name": name, "focused": bool(focus), "held": 0.4}
        except OSError as e:
            return {"ok": False, "error": "Qud bridge :%s unreachable (%s)" % (port, e)}

    # A live-game probe result, cached POSITIVELY only. port -> expiry timestamp.
    _LIVE_TTL = 2.0

    def _game_live(self, port):
        """(port_open, game_live) for the mod bridge.

        The mod's listener is open even at Qud's MAIN MENU (it starts at load), so port-open
        alone cannot tell menu from in-game. But the mod force-publishes a snapshot on connect
        ONLY when a game is live, so a brief read is the true liveness signal: bytes -> a game
        is running; silence -> a menu screen. Mirrors MainMenu's own probe.

        CACHED, AND ONLY WHEN TRUE. This probe is a real bridge CLIENT -- it connects, reads a
        byte and drops -- and `hv state` runs it on every evaluation, which the cockpit does
        about twice a second. Every one of those connects fires the mod's OnConnect, which
        re-announces any live popup to every connected client (raves-of-qud PopupBridge) and
        fills Qud's Player.log with connected/disconnected/"dropped slow client" for as long as
        the modal is up. Two of that churn's consequences were real bugs: half-typed AskString
        text resetting, and an option list's selection springing back to the first row.

        The asymmetry is the one the tree already rests on: bytes mean a game is running, and
        that does not stop being true within two seconds. Silence means nothing on its own and
        is exactly what a caller waiting for `title -> in_game` needs re-measured every poll.
        So an ARRIVAL is never delayed by this cache; only a departure can read stale, and only
        for _LIVE_TTL.
        """
        import socket as _socket
        import time as _t
        cache = getattr(self, "_live_until", None)
        if cache is None:
            cache = self._live_until = {}
        if cache.get(port, 0.0) > _t.time():
            return True, True
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=0.4) as sk:
                sk.settimeout(0.35)
                try:
                    live = len(sk.recv(1)) > 0
                except (_socket.timeout, OSError):
                    live = False
        except OSError:
            cache.pop(port, None)
            return False, False
        if live:
            cache[port] = _t.time() + self._LIVE_TTL
        else:
            cache.pop(port, None)
        return True, live

    # ------------------------------------------------------- qud saves (from DISK)
    def _qud_saves(self):
        """The save list AND the Load Game picker's row order, read from disk —
        Qud writes Primary.json (name/location/mode/SaveTime) into each save dir at
        SAVE TIME, so no game launch is needed to know what the picker will show.
        Row order = mtime desc, matching the picker (verified against it)."""
        import json as _json
        import os as _os
        # Qud's data dir differs per OS (Unity persistentDataPath): the Mac uses
        # the bundle-id Library path, Windows uses AppData/LocalLow/<company>.
        if _os.name == "nt":
            qroot = _os.path.join(_os.path.expanduser("~"), "AppData", "LocalLow",
                                  "Freehold Games", "CavesOfQud")
        else:
            qroot = _os.path.expanduser(
                "~/Library/Application Support/com.FreeholdGames.CavesOfQud")
        root = _os.path.join(qroot, "Synced", "Saves")
        out = []
        try:
            for guid in _os.listdir(root):
                pj = _os.path.join(root, guid, "Primary.json")
                if not _os.path.isfile(pj):
                    continue
                try:
                    meta = _json.load(open(pj))
                except Exception:
                    meta = {}
                out.append({"guid": guid, "name": meta.get("Name", "?"),
                            "id": meta.get("ID", guid),
                            "location": meta.get("Location", "?"),
                            "mode": meta.get("GameMode", "?"),
                            "saved": meta.get("SaveTime", "?"),
                            "mtime": _os.path.getmtime(pj)})
        except OSError as e:
            return {"ok": False, "error": str(e)}
        out.sort(key=lambda s: -s["mtime"])
        for i, s in enumerate(out):
            s["row"] = i
        return {"ok": True, "saves": out}

    # ------------------------------------------------------- clean restart
    def _quit_app(self, b, app, force=False):
        """Stop EVERY instance of an app and leave it stopped.

        The gap this fills: highvisor could launch and restart, but not simply STOP — so
        "run this with only the other app up" had no expression here, and the only way to
        get it was hand-driving the app's own quit menu, which is what the prime rule
        exists to prevent. It came up chasing a quit-path failure that two apps mirroring
        each other's modals could plausibly explain: testing that means running one alone.

        TERM first, KILL only on `force` or after the grace period. A -9 skips the app's own
        shutdown, and both of these apps write state on the way out (Raves' UiState report,
        Qud's save) — losing that would make the next run's state file a lie, which is a
        worse failure than a slow quit. `restart_app` still uses -9 because it relaunches
        immediately and does not care what the dying instance was holding.

        Verified by the WINDOW going away, not by the signal being sent: pkill reports
        success for a process that ignores TERM.
        """
        import subprocess as _sp
        import time as _t
        from .apps import PROFILES
        prof = PROFILES.get(app) or {}
        proc, win = prof.get("proc"), prof.get("window", "")
        if not proc:
            return {"ok": False, "error": "no proc profile for app %r" % app}

        def up():
            return [t for t in b.list_targets()
                    if win and win in (t.to_dict().get("title") or "")]

        before = len(up())
        if not before:
            return {"ok": True, "app": app, "was_running": False, "detail": "not running"}

        import os as _os
        windows = _os.name == "nt"
        if windows:
            _sp.run(["taskkill"] + (["/F"] if force else []) + ["/IM", proc + ".exe"],
                    capture_output=True)
        else:
            _sp.run(["pkill", "-9" if force else "-TERM", "-f", proc], capture_output=True)

        deadline = _t.time() + (5 if force else 12)
        while _t.time() < deadline:
            if not up():
                return {"ok": True, "app": app, "was_running": True, "instances": before,
                        "forced": bool(force)}
            _t.sleep(0.5)

        if force:
            return {"ok": False, "app": app,
                    "error": "%s still has a window after a forced kill" % app}
        # graceful quit ignored or blocked (a modal can hold it) — say so, and say the remedy
        return {"ok": False, "app": app, "instances": before,
                "error": "%s did not exit within 12s of TERM — retry with --force" % app}

    def _restart_app(self, b, app):
        """Kill EVERY instance of the app (duplicates included — the double-launch
        class), launch its solo launcher, wait for the window. The one true restart."""
        import subprocess as _sp
        import time as _t
        from .apps import PROFILES
        prof = PROFILES.get(app) or {}
        proc, launcher, win = prof.get("proc"), prof.get("launcher"), prof.get("window", "")
        if not proc or not launcher:
            return {"ok": False, "error": "no proc/launcher profile for app %r" % app}
        import os as _os
        if _os.name == "nt":
            # taskkill matches the IMAGE NAME, and a profile's `proc` stem is the MAC
            # binary name. For qud that happens to match (CoQ.exe); for RAVES it does not
            # — a dev-run Raves IS the Godot binary, so `/IM RavesOfQud.exe` matched
            # nothing and "restart" quietly became "launch another one". That is how this
            # box ended up with three live Raves, at which point the duplicate-instance
            # guard (correctly) refused to drive anything.
            #
            # Kill by PID off the app's own WINDOWS instead. It gets duplicates, and it
            # cannot take out an unrelated Godot editor the way an image-name kill would.
            pids = set()
            for t in b.list_targets():
                d = t.to_dict()
                if win and win in (d.get("title") or ""):
                    if d.get("pid"):
                        pids.add(d["pid"])
            for pid in pids:
                _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            # Belt and braces for a windowless corpse the enumeration cannot see.
            _sp.run(["taskkill", "/F", "/IM", proc + ".exe"], capture_output=True)
        else:
            _sp.run(["pkill", "-9", "-f", proc], capture_output=True)
        deadline = _t.time() + 10
        while _t.time() < deadline:
            if not any(win in (t.to_dict().get("title") or "") for t in b.list_targets()):
                break
            _t.sleep(0.5)
        from .launch import resolve_launch
        try:
            spec, largs = resolve_launch(launcher)
        except KeyError as e:
            return {"ok": False, "error": str(e.args[0] if e.args else e)}
        if not spec:
            return {"ok": False, "error": "no launcher %r" % launcher}
        launched_at = _t.time()
        b.launch(spec, largs)
        deadline = _t.time() + 45
        appeared = False
        while _t.time() < deadline:
            if any(win in (t.to_dict().get("title") or "") for t in b.list_targets()):
                appeared = True
                break
            _t.sleep(1.0)
        if not appeared:
            return {"ok": False, "launched": spec, "window": None,
                    "error": "window never appeared"}
        try:
            self._dock(b, win)   # standing slot rule, best-effort
        except Exception:
            pass
        # A WINDOW IS NOT READINESS: Godot puts one up in about a second, before the
        # app has loaded settings, connected or reported anything. Returning on the
        # window alone gave callers a postcondition they could not use -- "restart
        # succeeded" told you nothing about whether driving it would work.
        #
        # This waits for a report written AFTER we launched, which is the difference
        # between the new process's first word and the dead one's last (freshness
        # alone cannot tell them apart: the corpse's write is only a second old).
        #
        # HONEST SCOPE: this was written to fix an intermittent restart->goto->assert
        # failure, and it does NOT demonstrably do so -- by the time it existed the
        # failure had stopped reproducing (6 trials, warm and cold, with the gate
        # both on and off, all passed; and no ghost report was observable either).
        # The flakiness most likely came from the popup re-announce churn fixed in
        # raves-of-qud cd62ff8, which had Qud dumping a GPU texture twice a second.
        # Kept because the postcondition is strictly better and costs ~0.7s, not
        # because it is a proven fix.
        reporting = self._await_report(app, launched_at)
        # RESTORE THE STAGE. A relaunched app does not come back where it was -- a Godot
        # dev-run Raves opens at the display default (4267x2400 here), which then reaches
        # a capture as a 2400-tall PNG scored against a 1080-tall spec. The stage owns
        # window geometry, so re-apply the layout rather than teach the app a size.
        # Best-effort and after readiness: a window that exists can still be resized, but
        # placing one mid-load has it move again when the app finishes settling.
        relaid = None
        try:
            from .layouts import last_layout
            _lay = last_layout()
            if _lay:
                _r = self._apply_layout(b, _lay)
                relaid = {"layout": _lay, "applied": _r.get("applied"),
                          "ok": bool(_r.get("ok"))}
        except Exception as e:
            relaid = {"error": str(e)}
        return {"ok": True, "launched": spec, "window": win,
                "reporting": reporting,
                "relaid": relaid,
                "error": None if reporting
                         else "window up but the app never reported a scene; "
                              "driving it now would steer by the previous process"}

    def _await_report(self, app, since, timeout=60.0):
        """Block until `app`'s state file carries a write NEWER than `since`.

        mtime > launch time is what separates the new process's first report from the
        dead one's last -- freshness alone cannot, because the corpse's write is only
        a second or two old and looks perfectly fresh.
        """
        import os as _os
        import time as _t
        from . import gametree
        cfg = (gametree.apps(gametree.load_tree()).get(app) or {})
        path = cfg.get("state_file")
        if not path:
            return None          # app authors no report; nothing to wait for
        p = _os.path.expanduser(path)
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            try:
                if _os.path.getmtime(p) > since:
                    return True
            except OSError:
                pass
            _t.sleep(0.25)
        return False

    # ------------------------------------------------------- load save BY NAME
    # The modal `loadsave` is allowed to answer, and the only one. Matched on the popup's own
    # text, and the option picked by ITS text -- both halves matter. Qud's pre-selected option
    # is "Restart using save game's mod configuration", i.e. relaunch with OUR BRIDGE OFF, and
    # that is what a blind default-press or a hardcoded index lands on when the modal is not
    # the one we assumed. It cost a debugging round on Lumpy already (bridge port shut,
    # heartbeat frozen, ModSettings.json flipped to Enabled:false).
    _MODCFG_MATCH = "mod configuration"
    _MODCFG_CHOOSE = "keeping current"

    def _answer_mod_config_popup(self, port):
        """Look at the live Qud modal; answer it ONLY if it is "Mod Configuration Differs".

        -> {answered: bool, chose?: str, saw?: str, error?: str}

        The mod re-publishes the active popup to every joining client, so connecting IS the
        read: no extra command, no guessing from the scene name.
        """
        import json as _json
        import socket as _socket
        import struct as _struct
        import time as _t
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                buf = b""
                frame = None
                deadline = _t.time() + 1.2
                while _t.time() < deadline and frame is None:
                    s.settimeout(max(0.05, deadline - _t.time()))
                    try:
                        chunk = s.recv(65536)
                    except (_socket.timeout, OSError):
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= 4:
                        n = _struct.unpack(">I", buf[:4])[0]
                        if len(buf) < 4 + n:
                            break
                        body, buf = buf[4:4 + n], buf[4 + n:]
                        try:
                            f = _json.loads(body.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if f.get("type") == "popup" and f.get("active"):
                            frame = f
                            break
                if frame is None:
                    return {"answered": False}
                text = ((frame.get("message") or "") + " " + (frame.get("title") or "")).lower()
                opts = [o.get("text") or "" for o in frame.get("options") or []]
                if self._MODCFG_MATCH not in text:
                    return {"answered": False,
                            "saw": (frame.get("message") or frame.get("title") or "?")[:120]}
                idx = next((i for i, o in enumerate(opts)
                            if self._MODCFG_CHOOSE in o.lower()), None)
                if idx is None:
                    # The right modal, but not the option we expect. Do NOT fall back to a
                    # position -- Qud's own default here disables the mod.
                    return {"answered": False, "saw": frame.get("message", "")[:120],
                            "error": "Mod Configuration popup has no %r option; saw %s"
                                     % (self._MODCFG_CHOOSE, opts)}
                p = _json.dumps({"type": "command", "name": "popup",
                                 "action": "option", "index": idx}).encode("utf-8")
                s.sendall(_struct.pack(">I", len(p)) + p)
                return {"answered": True, "chose": opts[idx]}
        except OSError as e:
            return {"answered": False, "error": "qud bridge: %s" % e}

    def _load_save(self, b, name):
        """Load a NAMED Qud save via the mod's `loadsave {id}` bridge command — exact
        ID match, no coordinate clicks, no focus stealing. (The old row-click drive
        loaded the wrong save when the picker's order drifted from disk mtime order.)
        The mod completes Qud's own picker completionSource, opening the picker itself
        from the title if needed; it REFUSES while a game is live, so restart first."""
        import json as _json
        import socket as _socket
        import struct as _struct
        import time as _t
        saves = self._qud_saves()
        if not saves.get("ok"):
            return saves
        sid = next((s["id"] for s in saves["saves"] if s["name"] == name), None)
        if sid is None:
            return {"ok": False, "error": "no save named %r" % name,
                    "have": [s["name"] for s in saves["saves"]]}
        st = (self._gamestate(b).get("states", {}).get("qud") or {})
        # Restart on a LIVE GAME even when the tree says title: after an unfocused load
        # Qud's view (and scene report) can stay "MainMenu" while a game runs — the mod
        # refuses loadsave mid-game, so trust the game_live probe over the scene.
        if st.get("node") != "title" or (st.get("signals") or {}).get("game_live"):
            r = self._restart_app(b, "qud")
            if not r.get("ok"):
                return {"ok": False, "error": "restart failed", "detail": r}
            _t.sleep(8)   # title settle after the window appears
        from .apps import PROFILES
        port = PROFILES.get("qud", {}).get("port", 48710)
        payload = _json.dumps({"type": "command", "name": "loadsave", "id": sid}).encode("utf-8")
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(_struct.pack(">I", len(payload)) + payload)
        except OSError as e:
            return {"ok": False, "error": "Qud bridge :%s unreachable (%s)" % (port, e)}
        deadline = _t.time() + 40
        answered = None       # the option LABEL we pressed, once we have pressed one
        unknown = None        # a modal we declined to answer, kept for the failure report
        while _t.time() < deadline:
            stq = (self._gamestate(b).get("states", {}).get("qud") or {})
            if stq.get("node") == "in_game":
                r = {"ok": True, "name": name, "id": sid, "via": "bridge loadsave"}
                if answered:
                    r["popup"] = "answered %r" % answered
                return r
            # "Mod Configuration Differs" — a save made without the bridge stops the load
            # dead on a popup whose PRE-SELECTED option is "Restart using save game's mod
            # configuration", i.e. relaunch with OUR BRIDGE DISABLED. Anything that blindly
            # presses the default here silently loses the mod for every later run; that is
            # not hypothetical, it happened on Lumpy and cost a debugging round (bridge
            # port shut, heartbeat frozen, ModSettings.json flipped to Enabled:false).
            #
            # Answered by CONTENT and chosen by LABEL, never by a hardcoded index. The scene
            # name is only the cheap trigger to go and look: Qud's heartbeat sets `scene` from
            # the raw view name, so *every* Qud popup satisfies "popup" in it, and index 1 is a
            # different button on a different modal. Pressing the wrong option on a modal
            # during a load is not a cosmetic failure, and CLAUDE.md already says it: fire ONE
            # answer and verify, never a fallback shotgun.
            if not answered and "popup" in str(
                    (stq.get("signals") or {}).get("scene") or "").lower():
                got = self._answer_mod_config_popup(port)
                if got.get("answered"):
                    answered = got["chose"]
                elif got.get("saw"):
                    # A modal we do not recognise. Leave it alone and say what it was --
                    # this path used to press index 1 and hope.
                    unknown = got["saw"]
            _t.sleep(1.5)
        # A closed bridge here means the mod got switched off — say so, because the
        # symptom (stale heartbeat, "no [raves] lines") points nowhere near the cause.
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
            hint = ""
        except OSError:
            hint = (" — and the Qud bridge is CLOSED: the mod looks disabled. Check "
                    "Local/ModSettings.json for RavesOfQudBridge.Enabled")
        if unknown and not answered:
            # Naming it is the whole value: the failure is "a modal we do not know is up",
            # not "the load is slow", and the old code could not tell those apart because
            # it answered whatever was there.
            hint += (" — an unrecognised Qud modal is up and this path will not answer it: "
                     "%r. Only \"Mod Configuration Differs\" is answerable here." % unknown)
        return {"ok": False, "error": "load did not reach in_game" + hint,
                "name": name, "id": sid, "popup_answered": answered,
                "popup_declined": unknown}

    def _gamestate(self, b, ocr=False):
        """Evaluate the game state-machine tree against live signals for each app.

        Cheap by default — window presence + Qud's 48710 bridge port. Pass ocr=True to
        also OCR each present window so menu-side screens (title / load / chargen) can be
        told apart; that path is heavier, so the cockpit polls it on a slower cadence.
        See gametree.py for the matching rules."""
        from . import gametree
        import socket
        tree = gametree.load_tree()
        wins = b.list_targets()
        states = {}
        for app, cfg in gametree.apps(tree).items():
            win = self._find_win(wins, cfg.get("window"))
            # DUPLICATE INSTANCES are reported, never silently averaged over. _find_win
            # takes the first match, so with two windows up we may drive one and read the
            # other; the pid below at least ties the scene report to the window we picked.
            dupes = len(self._all_wins(wins, cfg.get("window")))
            wpid = win.pid if win is not None else None
            signals = {"present": win is not None, "port_open": None,
                       "game_live": None, "ocr_text": None,
                       "scene": self._read_scene(cfg.get("state_file"), wpid),
                       "report": self._report_health(cfg.get("state_file"), wpid),
                       "tab": self._read_tab(cfg.get("state_file"), wpid)}
            port = cfg.get("port")
            if port:
                signals["port_open"], signals["game_live"] = self._game_live(int(port))
            if ocr and win is not None:
                try:
                    res = b.ocr(win.id)
                    signals["ocr_text"] = "\n".join(x.get("text", "") for x in res.get("boxes", []))
                except Exception:
                    signals["ocr_text"] = None
            st = gametree.evaluate(tree, app, signals)
            st["window"] = win.to_dict() if win else None
            st["signals"] = {"present": signals["present"], "port_open": signals["port_open"],
                             "game_live": signals["game_live"],
                             "scene": signals["scene"], "report": signals["report"],
                             "tab": signals["tab"],
                             "pid": wpid, "instances": dupes,
                             "ocr_used": signals["ocr_text"] is not None}
            st["extra"] = self._read_state_extra(cfg.get("state_file"), wpid)
            states[app] = st
        return {"ok": True, "ocr": bool(ocr), "states": states}

    # App-authored state files — first-party scene reports (the Qud mod's qud_state.json,
    # Raves' raves_state.json). Far more accurate than OCR and cheap enough for every poll.
    # A report is trusted only while FRESH (mtime within STATE_FILE_TTL): a crashed app's
    # last write must not pin the tree to a stale screen.
    STATE_FILE_TTL = 6.0

    def _settle_rendering(self, b, target, max_age=2.0, timeout=30.0):
        """Wait until the target app is actually RENDERING, re-activating as needed.

        A Unity app that is not rendering still screenshots -- it hands back its last
        frame, and for Qud that frame is the playfield WITHOUT the UI overlay, so a
        status-screen capture comes back looking like the plain map. The heartbeat is
        not lying when this happens; it correctly reports the status screen. The
        capture is the thing that is stale, which is why this survived so long: every
        state check agreed with what we wanted while the PNG on disk did not.

        `ui_age` is the tell, and it has to be read AT the capture, not around it.
        Measured 2026-08-08: at ui_age 1 the file holds the real screen, at 5..23 it
        holds a stale UI-less frame -- and runs that sampled ui_age before or after
        the shot happily logged 1 while the shot itself was stale.

        Re-activating matters as much as waiting: `activate` frequently does not take
        when another window is contending (three attempts in a row measured before one
        landed), so this retries it rather than trusting one call and sleeping.

        Returns a dict merged into the shot response: whether it settled and the age
        it settled at. Never raises and never blocks the capture -- a stale shot that
        is LABELLED stale is still better than no shot, and the caller decides.
        """
        import os as _os
        import time as _t
        from . import gametree
        try:
            apps = gametree.apps(gametree.load_tree())
        except Exception:
            return {"live_checked": False, "live_reason": "no tree"}
        # Match the target against each app's window name; the shot target is a window.
        want = str(target or "").strip().lower()
        cfg = None
        for _name, _cfg in apps.items():
            win = str(_cfg.get("window") or "").strip().lower()
            if win and (win == want or win in want or want in win):
                cfg = _cfg
                break
        path = (cfg or {}).get("state_file")
        if not path:
            return {"live_checked": False, "live_reason": "app authors no state file"}
        p = _os.path.expanduser(path)

        # READ THE PID'S OWN SIDECAR. The shared state file has one writer per running
        # instance, so with duplicates a bare read is a coin flip -- which is the exact
        # failure the per-process sidecars were added for, and this call was bypassing them.
        # Only the ui_age is wrong when that happens, not the screen, but a wrong ui_age is
        # what decides whether a capture is labelled live, so it is worth the lookup.
        pid = None
        try:
            w = self._find_win(b.list_targets(), target)
            pid = getattr(w, "pid", None) if w is not None else None
        except Exception:
            pid = None

        deadline = _t.time() + timeout
        last = None
        tries = 0
        while _t.time() < deadline:
            d = self._read_state_file(p, pid)
            age = (d or {}).get("ui_age")
            if age is None:
                return {"live_checked": False, "live_reason": "state file has no ui_age"}
            last = age
            if float(age) <= max_age:
                return {"live_checked": True, "live": True,
                        "ui_age": age, "live_tries": tries}
            tries += 1
            try:
                b.activate(target)
            except Exception:
                pass
            _t.sleep(1.2)
        return {"live_checked": True, "live": False, "ui_age": last,
                "live_tries": tries,
                "live_reason": "ui_age stayed above %s for %ss -- this capture is "
                               "probably a stale frame" % (max_age, timeout)}

    def _read_state_file(self, path, pid=None):
        """Parsed JSON dict of a fresh state file, else None.

        `pid` is the process that OWNS the window we are evaluating. It matters because
        the shared path has one writer per running instance: three live Raves processes
        had raves_state.json cycling in_game -> status_tinkering -> title every two
        seconds, so a single read was a coin flip and `hv state` confidently reported a
        screen the window in front of us was not on. Given a pid we read that process's
        own sidecar (raves_state.<pid>.json) and are immune to duplicates.

        The shared file is still the fallback, but only when it does not CONTRADICT the
        pid: a report stamped with somebody else's pid is a foreign window's, and None
        (detection falls back to OCR/port) beats a confident wrong answer. Reports with
        no pid key -- the Qud mod's qud_state.json -- are read exactly as before.
        """
        import json as _json
        import os as _os
        import time as _time
        if not path:
            return None
        base = _os.path.expanduser(path)
        cands = [base]
        if pid:
            stem, ext = _os.path.splitext(base)
            cands.insert(0, "%s.%d%s" % (stem, int(pid), ext))
        for p in cands:
            try:
                if _time.time() - _os.path.getmtime(p) > self.STATE_FILE_TTL:
                    continue
                with open(p, "r", encoding="utf-8") as fh:
                    d = _json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            if pid and d.get("pid") and int(d["pid"]) != int(pid):
                continue     # another instance's report — refuse rather than guess
            return d
        return None

    def _report_health(self, path, pid=None):
        """Why the first-party report was or was not usable: fresh | stale | foreign | absent.

        `_read_state_file` already decides this and then throws the reason away, returning a
        bare None — so a refused report and an app that simply matched no scene looked
        identical downstream, and `title`'s `{game_live: false}` alternative quietly claimed a
        screen on the strength of an inference. Measured twice: "Title Screen via=live" for 7
        minutes while Qud sat on the Modding Toolkit with the mod unloaded, and again on a
        parked Keybinds screen that had a LIVE game behind it. Both confident, both wrong.

        Keeping the reason as a SIGNAL (rather than a log line or a `via` string) is what makes
        it decidable statically: signals in, state out, so selftest_evaluate can drive it with
        no daemon and no apps.
        """
        import os as _os
        import time as _time
        import json as _json
        if not path:
            return "absent"
        base = _os.path.expanduser(path)
        cands = [base]
        if pid:
            stem, ext = _os.path.splitext(base)
            cands.insert(0, "%s.%d%s" % (stem, int(pid), ext))
        worst = "absent"
        for p2 in cands:
            try:
                age = _time.time() - _os.path.getmtime(p2)
            except OSError:
                continue
            if age > self.STATE_FILE_TTL:
                worst = "stale"
                continue
            try:
                with open(p2, "r", encoding="utf-8") as fh:
                    d = _json.load(fh)
            except (OSError, ValueError):
                worst = "stale"
                continue
            if not isinstance(d, dict):
                worst = "stale"
                continue
            if pid and d.get("pid") and int(d["pid"]) != int(pid):
                worst = "foreign"
                continue
            return "fresh"
        return worst

    def _read_scene(self, path, pid=None):
        d = self._read_state_file(path, pid)
        return d.get("scene") if d else None

    def _read_tab(self, path, pid=None):
        """The active SUB-SCREEN within the reported scene (Qud's status-screen tab)."""
        d = self._read_state_file(path, pid)
        return d.get("tab") if d else None

    def _read_state_extra(self, path, pid=None):
        """The full fresh report minus the scene key (mode, popup, zone, …) for the UI."""
        d = self._read_state_file(path, pid)
        if not d:
            return None
        return {k: v for k, v in d.items() if k != "scene"}

    # ----------------------------------------------------------- assert (TDD)
    def _assert_state(self, b, req):
        """Poll the live state until the requested condition holds, or time out.

        The TDD primitive for state-dependent work: ``hv assert --app qud --node in_game
        --timeout 20`` blocks until Qud reports in-game (exit 0) or dumps the actual
        state (exit 1). Conditions (all supplied must hold):
          app + node:     the app's current node == node, or node is on its path
          exact:          with `node`, drop that path tolerance — the node must be EXACT
          not_within:     fail if the current node is this node or any descendant of it
          scene:          the app's self-reported scene equals this
          popup:          true = any popup up (state-file ``popup`` key), or a popup type
          present:        window presence equals this bool
          ocr_contains:   the app window's OCR contains this substring (heavy — forces OCR)
          report:         {key: value} over the app's OWN first-party report — the same
                          dict `hv state` shows as `extra` (Raves: mode/snap_ts/ui_age/…,
                          Qud's mod: whatever its heartbeat writes). `true` means "present
                          and truthy", `false` means "absent or falsy", anything else is
                          compared as a string. This is the general form of the rule the
                          tree already lives by: when detection needs to know something,
                          have the app REPORT it and condition on the report, rather than
                          inferring it from a screen or waiting out a sleep.
        ``ok`` = the op ran; ``passed`` = the assertion's verdict."""
        import time
        app = req.get("app")
        want = {k: req[k] for k in ("node", "scene", "popup", "present", "ocr_contains",
                                    "not_within", "report") if k in req and req[k] is not None}
        if req.get("exact"):
            want["exact"] = True   # a MODIFIER on `node`, not a condition of its own
        if not app or not [k for k in want if k != "exact"]:
            return {"ok": False, "error": "assert needs app and at least one condition"}
        timeout = float(req.get("timeout", 10.0))
        interval = max(0.2, float(req.get("interval", 0.8)))
        need_ocr = "ocr_contains" in want
        t0 = time.monotonic()
        actual = None
        while True:
            st = self._gamestate(b, ocr=need_ocr).get("states", {}).get(app)
            actual = st
            if st is not None and self._assert_holds(want, st):
                return {"ok": True, "passed": True, "app": app, "want": want,
                        "elapsed": round(time.monotonic() - t0, 2), "actual": _slim_state(st)}
            if time.monotonic() - t0 >= timeout:
                res = {"ok": True, "passed": False, "app": app, "want": want,
                       "elapsed": round(time.monotonic() - t0, 2), "actual": _slim_state(st),
                       "error": "assert timed out"}
                # SURFACE ui_age. It was already in the payload — twenty lines down inside
                # actual.extra — which is not where anyone looks when an assert fails, and
                # that is not hypothetical: a stalled Qud uiQueue was misdiagnosed as a bad
                # recipe TWICE on this branch in one day, the second time by the person who
                # had written the gotcha that morning. A stale UI and a wrong recipe are
                # indistinguishable from the destination state; only this number separates
                # them, so it gets promoted to the top level and says what it means.
                age = ((st or {}).get("extra") or {}).get("ui_age")
                if age is not None:
                    res["ui_age"] = age
                    if isinstance(age, (int, float)) and age > 10:
                        res["error"] = (
                            "assert timed out — but the app's UI is STALE (ui_age %s). Its "
                            "queue has stopped draining, so mod-driven steps no-op silently "
                            "and the state never moves. Restart the app before suspecting "
                            "the recipe." % age)
                return res
            time.sleep(interval)

    def _assert_holds(self, want, st):
        if "present" in want:
            if bool(st.get("signals", {}).get("present")) != bool(want["present"]):
                return False
        if "node" in want:
            node = want["node"]
            # The path tolerance is DIRECTIONAL, and it has to be. Detection reports the
            # DEEPEST match, so an edge aiming at a container legitimately lands on a child:
            # raves title->new_game arrives on the first chargen card (game_mode), and
            # demanding the exact node there would fail a drive that worked.
            #
            # Going the other way the same tolerance is poison. `assert node=map_editor`
            # is satisfied by me_menu_file — the very state an escape edge is supposed to
            # LEAVE — so that edge's verify could not fail, and `hv goto` reported success
            # having moved nothing (measured 2026-08-07; the edge was dropped rather than
            # shipped). `exact` and `not_within` are the two ways to say "and it actually
            # moved"; _drive_route sets not_within by itself for any edge that climbs.
            if want.get("exact"):
                if st.get("node") != node:
                    return False
            elif st.get("node") != node and node not in (st.get("path") or []):
                return False
        if "not_within" in want:
            inside = want["not_within"]
            if st.get("node") == inside or inside in (st.get("path") or []):
                return False
        if "scene" in want:
            if (st.get("signals", {}).get("scene") or "") != want["scene"]:
                return False
        if "popup" in want:
            # Engine., not self. -- `_assert_holds` is deliberately pure over (want, state)
            # so tools/selftest_evaluate.py can exercise it with no daemon and no instance.
            if not Engine._popup_matches((st.get("extra") or {}).get("popup"), want["popup"]):
                return False
        if "report" in want:
            # The app's own report, which is the most trustworthy signal we have -- but only
            # a POSITIVE one. A key that is absent because the reporter is broken looks
            # exactly like a key that is absent because the thing is not true (highvisor's
            # CLAUDE.md: keep the positives, distrust the negatives), so `false` here means
            # "not currently reporting it" and must not be read as proof of the opposite.
            extra = st.get("extra") or {}
            for key, val in (want["report"] or {}).items():
                got = extra.get(key)
                if val is True:
                    if not got:
                        return False
                elif val is False:
                    if got:
                        return False
                elif str(got) != str(val):
                    return False
        if "ocr_contains" in want:
            # _gamestate stored no raw text; re-derive from the evaluate input is overkill —
            # OCR the window directly (need_ocr already made the poll heavy anyway).
            win = st.get("window")
            if not win:
                return False
            try:
                res = self.backend.ocr(win["id"])
                text = "\n".join(x.get("text", "") for x in res.get("boxes", [])).lower()
            except Exception:
                return False
            if str(want["ocr_contains"]).lower() not in text:
                return False
        return True

    # ------------------------------------------------------- goto tracing
    # Every goto run appends one record here: the state it STEERED BY on entry, each
    # step's outcome, and the state it left behind. Written for the failure that could
    # not be diagnosed after the fact -- a goto that reported ok because the app was
    # "already at" a node it had actually just left, followed by an assert that failed
    # with nothing to show why. A trivial success and a real one look identical in the
    # return value; they do not look identical here.
    TRACE_PATH = "~/.config/highvisor/goto-trace.jsonl"
    TRACE_KEEP = 400          # lines; a bounded ring so it can be left on forever

    def _trace(self, record):
        import json as _json
        import os as _os
        import time as _time
        try:
            p = _os.path.expanduser(self.TRACE_PATH)
            _os.makedirs(_os.path.dirname(p), exist_ok=True)
            record = dict(record, t=_time.strftime("%Y-%m-%dT%H:%M:%S"))
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(record) + "\n")
            # trim in place, cheaply, only when it has grown well past the cap
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
                if len(lines) > self.TRACE_KEEP * 2:
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.writelines(lines[-self.TRACE_KEEP:])
            except OSError:
                pass
        except Exception:
            pass          # tracing must never break a drive

    def _read_trace(self, limit=20):
        import json as _json
        import os as _os
        p = _os.path.expanduser(self.TRACE_PATH)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-int(limit):]
        except OSError:
            return {"ok": True, "runs": [], "path": p, "note": "no trace yet"}
        runs = []
        for ln in lines:
            try:
                runs.append(_json.loads(ln))
            except ValueError:
                pass
        return {"ok": True, "runs": runs, "path": p}

    # ------------------------------------------------------- planner (route-to-state)
    def _planner_state(self, st):
        """Which PLANNER state the app is in — the tree node, or one of the two
        non-node states the graph needs so that "get me out of here" always has a
        starting point: ``off`` (no window) and ``unknown`` (window up, nothing matched)."""
        from . import plan
        if not st or st.get("off"):
            return plan.OFF
        return st.get("node") or plan.UNKNOWN

    def _when_holds(self, when, st):
        """Does a preflight rule's ``when`` block describe the app's current state?"""
        sig = (st or {}).get("signals") or {}
        extra = (st or {}).get("extra") or {}
        for key, want in (when or {}).items():
            have = sig.get(key) if key in sig else extra.get(key)
            want_list = want if isinstance(want, list) else [want]
            if str(have or "").lower() not in [str(w).lower() for w in want_list]:
                return False
        return True

    @staticmethod
    def _popup_matches(have, want):
        """Does the app's reported modal KIND satisfy a `popup` condition?

        One matcher for all three callers (`hv assert`, an `assert` step, a `dismiss`
        condition) because they were drifting: the assert path already understood
        `popup: true` = "any modal", the dismiss path compared strings only, so
        `{"dismiss": {"popup": true}}` silently never matched.

        Naming ONE kind is the trap this exists to make avoidable. Raves reports
        `message` / `menu` / `input` / `itempicker` / `feedback`, so a condition that
        says `message` is stepped past by any of the other four -- which is exactly how
        Qud's ABANDON confirm (an AskString, mirrored as `input`) walked through all
        four steps of `raves in_game -> title` while every one reported ok. Accepts:

            true              any modal at all -- the kind-agnostic form, prefer it for
                              "clear whatever is in the way" steps
            "message"         that kind
            ["message","menu"] any of these
        """
        have = str(have or "")
        if want is True:
            return bool(have)
        if want is False:
            return not have          # "and nothing is up" -- assertable, so a chain can
                                     # end by proving it left no modal behind
        if isinstance(want, (list, tuple)):
            return have.lower() in [str(w).lower() for w in want]
        return have.lower() == str(want).lower()

    @staticmethod
    def _dismiss_fingerprint(st, scene=None):
        """What "the screen changed" MEANS for a dismiss step.

        The kind of popup is not enough to tell one modal from the next of the same kind, and
        the quit chain is exactly that: "are you sure you want to quit?" then "do you want to
        save first?", both reported as `message`. Answering the first raises the second, the
        (scene, popup) pair comes back identical, and the step reports "dismiss ran but popup
        message is still up" about an answer that landed — measured 2026-08-07, and it failed
        the whole route roughly every other attempt depending on which confirm was up when the
        drive started.

        `popup_n` is Raves' count of popups RAISED this run, so a replacement modal moves it.
        Apps that do not report one degrade to the old pair, which is what Qud does (it reports
        no popup state of its own, which is why its edges answer blind chains instead).
        """
        sig = st.get("signals") or {}
        extra = st.get("extra") or {}
        return (str(scene if scene is not None else (sig.get("scene") or "")).lower(),
                str(extra.get("popup") or "").lower(),
                str(extra.get("popup_n") or ""))

    def _preflight(self, b, app, tree):
        """Clear conditions that sit ON TOP of a state, before planning against it.

        A ghost modal is not a place you can be — it is something that can be true
        anywhere, and it silently eats every key while it is. Planning a route while one is
        up produces a perfectly good route whose first keypress vanishes. So: clear first,
        re-read, then plan against what is actually there. See plan.preflight for why this
        is a guard and not a graph edge.
        """
        from . import plan
        done = []
        for rule in plan.preflight(tree, app):
            cur = self._gamestate(b).get("states", {}).get(app) or {}
            if not self._when_holds(rule.get("when"), cur):
                continue
            for step in rule.get("steps") or []:
                r = self._run_step(b, app, tree, step)
                done.append({"step": step, "ok": r.get("ok"), "detail": "preflight",
                             "error": r.get("error")})
                if not r.get("ok"):
                    break
        return done

    def _drive_route(self, b, app, tree, route, steps, _depth, start=None):
        """Run a planned route, edge by edge, verifying every arrival.

        The per-edge verify is not belt-and-braces — it is what makes re-planning safe.
        An edge that ran its steps without erroring has not necessarily ARRIVED (a click
        that landed on nothing errors nowhere), and a route that continues from a state it
        merely assumes fails several steps later somewhere unrelated. Checking at every
        edge means a failure names the transition that actually broke.

        ``start`` is the state the route was planned from, so each edge knows where it set
        off — which is what lets a CLIMBING edge (one whose target contains its origin)
        demand that it actually left. Without that, its verify is vacuous; see
        ``_assert_holds``. The chain is the planner's own: after edge N we are at its ``to``.
        """
        from . import plan
        prev = start
        for edge in route:
            self.bus.publish("gamego", app=app, node=edge.get("to"),
                             step={"transition": edge.get("id"), "cost": edge.get("cost")})
            for step in edge.get("steps") or []:
                r = self._run_step(b, app, tree, step, _depth)
                steps.append({"step": step, "ok": bool(r.get("ok")), "edge": edge.get("id"),
                              "detail": r.get("detail", ""), "error": r.get("error")})
                if not r.get("ok"):
                    # `refused` travels as a FLAG with the edge id attached. The tour script
                    # already had to string-match this once and broke on letter case; nothing
                    # downstream should ever parse an error message to learn it happened.
                    return {"ok": False, "refused": bool(r.get("refused")),
                            "refused_edge": edge.get("id") if r.get("refused") else None,
                            "error": "%s: %s" % (edge.get("id"), r.get("error"))}
            a = dict(edge.get("verify") or {"node": edge.get("to")})
            a.setdefault("app", app)
            a["timeout"] = edge.get("timeout", 15)
            # An edge that CLIMBS — target contains origin — cannot be verified by the node
            # check alone, because staying put satisfies it. Say so, unless the author
            # already pinned it with `exact` or their own `not_within`.
            if ("node" in a and prev and not a.get("exact") and "not_within" not in a
                    and prev != a["node"] and prev in plan.subtree_ids(tree, a["node"])):
                a["not_within"] = prev
            ar = self._assert_state(b, a)
            steps.append({"step": {"verify": a}, "ok": bool(ar.get("passed")),
                          "edge": edge.get("id"), "actual": ar.get("actual")})
            if not ar.get("passed"):
                return {"ok": False, "error": "%s did not arrive: wanted %s, got %s"
                        % (edge.get("id"), edge.get("verify"),
                           (ar.get("actual") or {}).get("label"))}
            prev = edge.get("to")
        return {"ok": True}

    # ------------------------------------------------------------- registered checks
    #: Where a test's ``cwd`` resolves. Repos are SIBLINGS on both machines, so a bare name
    #: resolves next to this checkout rather than against a machine-local path in the tree —
    #: gametree.json is committed and shared with the PC branch.
    TEST_TIMEOUT = 600

    def _run_test(self, node_id, test_id):
        """Run a check REGISTERED IN THE TREE, by id. Never an arbitrary command.

        The caller names which check; the command text lives in gametree.json under version
        control, next to the state it covers. That is what makes "run this node's check" safe
        to expose from a UI — it can never become "run this string".
        """
        import os as _os
        import subprocess as _sp
        import time as _t
        from . import gametree
        tree = gametree.load_tree()
        test = gametree.find_test(tree, node_id, test_id)
        if test is None:
            have = ["%s/%s" % (n or "-", t.get("id")) for n, t in gametree.all_tests(tree)]
            return {"ok": False, "error": "no registered test %r on %r"
                    % (test_id, node_id or "the harness"), "have": have}
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        want = test.get("cwd") or ""
        cwd = repo if want in ("", "highvisor") else _os.path.join(_os.path.dirname(repo), want)
        if not _os.path.isdir(cwd):
            return {"ok": False, "error": "cwd %r for test %r does not exist (expected a sibling "
                    "of the highvisor checkout)" % (cwd, test_id)}
        started = _t.time()
        try:
            r = _sp.run(test["cmd"], shell=True, cwd=cwd, capture_output=True, text=True,
                        timeout=self.TEST_TIMEOUT)
            out, err, code = r.stdout, r.stderr, r.returncode
        except _sp.TimeoutExpired:
            return {"ok": False, "test": test_id, "node": node_id, "timeout": self.TEST_TIMEOUT,
                    "error": "timed out after %ds" % self.TEST_TIMEOUT}
        secs = round(_t.time() - started, 1)
        # TAIL, not head: a check that fails says so at the END (the summary line), and the
        # front of the output is the part you already know.
        tail = [ln for ln in (out + err).splitlines() if ln.strip()][-12:]
        return {"ok": code == 0, "test": test_id, "node": node_id, "exit": code,
                "seconds": secs, "cmd": test["cmd"], "cwd": cwd, "tier": test.get("tier"),
                "tail": tail,
                "detail": "%s in %.1fs" % ("passed" if code == 0 else "FAILED (exit %d)" % code, secs)}

    def _plan_route(self, b, app, node_id, start=None):
        """The route ``gamego`` WOULD take — without touching anything.

        A dry run is only useful because routing is now derived. Under the recipe model the
        answer to "what will this do?" was "read the recipe, then read the recipe it chains
        to"; here it is a search whose result can simply be printed. It is also how an
        unreachable target gets diagnosed before an app has been driven halfway there.

        ``start`` overrides the detected state, so a route can be checked for a screen we
        are not currently on — including with nothing running at all.

        With NO ``node_id`` it returns ``costs``: the cheapest cost to every reachable state
        at once. That is one call instead of one per node per app (132 on the current tree),
        and it is what lets Raves' panel grey the states it cannot drive to.
        """
        from . import gametree, plan
        tree = gametree.load_tree()
        if app not in gametree.apps(tree):
            return {"ok": False, "error": "unknown app %r" % app}
        signals = None
        if start is None:
            st = self._gamestate(b).get("states", {}).get(app) or {}
            start = self._planner_state(st)
            signals = st.get("signals")
        if not node_id:
            costs = plan.reachable(tree, app, start, signals=signals)
            return {"ok": True, "app": app, "from": start, "costs": costs,
                    "summary": "%d states reachable from %s" % (len(costs), start)}
        rt = plan.route(tree, app, start, node_id, signals=signals)
        rt["app"] = app
        rt["summary"] = plan.summarize(rt)
        if rt.get("ok"):
            rt["steps"] = [{"to": e.get("to"), "cost": e.get("cost"), "id": e.get("id"),
                            "steps": e.get("steps"), "verify": e.get("verify"),
                            "note": e.get("note")} for e in rt["route"]]
            rt.pop("route", None)
        return rt

    # ------------------------------------------------------- gamego (drive-to-state)
    def _gamego(self, b, app, node_id, _depth=0, _replans=0, _exclude=(), no_restart=False):
        """Drive ``app`` to tree state ``node_id``.

        PLANNED, not scripted. The route is searched at call time from the state the app is
        actually detected in, over the transition graph in gametree.json (see plan.py for
        why that beats the per-node recipes this replaced). Sequence:

          1. refuse to drive duplicate instances; return immediately if already there
          2. run preflight self-heals (ghost modals), then RE-READ the state
          3. plan a route from the detected state; an impossible target fails HERE,
             before anything has been touched
          4. execute each transition's steps and verify its arrival
          5. on a failed edge, re-read — if the app moved somewhere unexpected, RE-PLAN
             from there rather than continuing a route that no longer applies

        Step 5 is the capability the recipe model could not have: a recipe is a fixed
        sequence, so an unexpected screen mid-run could only be a failure.

        Legacy ``goto[app]`` recipes still run for any node the graph cannot reach, so a
        node not yet modelled as transitions keeps working. Steps (shared by both):
          {"goto": node}                  run that node's recipe first (recursion, depth-capped)
          {"launch": name, "unless_running": bool}   launch unless the app's window is up
          {"restart": app}                kill all instances, relaunch, wait for the report
          {"wait_window": label, "timeout": s}       poll for the window
          {"activate": label}             front the window
          {"hover": [x,y], "window": label}          move the cursor ONLY, no buttons —
                                                     for UIs where hovering selects and
                                                     clicking confirms (Qud's chargen)
          {"click_hover": [x,y], "window": label}    hover-click (menus need the hover)
          {"click": [x,y], "window": label}          plain click
          {"click_text": "label", "window": label}   OCR-locate the text, hover-click its
                                                     center — survives menu reflow (items
                                                     shift when Continue/Quick Start appear)
          {"key": keys, "window": label}  focused key injection
          {"command": name, "answers": [btn]}        a Qud command + the confirms it raises
          {"bridge": name, "args": {...}} first-party mod command
          {"dock": label}                 place the window by its standing dock rule
          {"dismiss": {...}}              conditional self-heal (see _run_step)
          {"sleep": s}                    settle pause
          {"assert": {...}, "timeout": s} inline _assert_state (app defaults to this app)
        """
        from . import gametree, plan
        if _depth > 4:
            return {"ok": False, "error": "goto recursion too deep (recipe cycle?)"}
        tree = gametree.load_tree()
        if app not in gametree.apps(tree):
            return {"ok": False, "error": "unknown app %r" % app}
        node = gametree.find_node(tree, node_id)
        if node is None:
            return {"ok": False, "error": "no tree node %r" % node_id}
        # already there? EXACT node only — ancestor containment lies here: detection
        # correctly reports records as title>menu_box>records, but being on the Records
        # SCREEN is not being on the title screen, and skipping the recipe strands us.
        st = self._gamestate(b).get("states", {}).get(app) or {}
        # REFUSE to drive duplicates. With two windows up, _find_win picks one and the
        # clicks go to whichever the OS fronts — so a recipe can drive window A, read
        # window B's state, and "fail" on a step that actually worked. That is what made
        # `hv goto raves in_game` need retries. Fail fast with the remedy instead: killing
        # instances from under the caller is not ours to decide (Raves' launcher owns Qud).
        _inst = (st.get("signals") or {}).get("instances") or 0
        if _inst > 1 and _depth == 0:
            return {"ok": False, "error": "%d instances of %s are running — driving one "
                    "while reading another is undefined. Run `hv restart %s` first."
                    % (_inst, app, app), "instances": _inst}
        entry = _slim_state(st)
        if st.get("node") == node_id:
            # The trivial success. Worth tracing precisely BECAUSE it runs no steps:
            # when it is wrong, it is wrong silently and the caller's next assert is
            # what fails.
            self._trace({"app": app, "node": node_id, "depth": _depth, "ok": True,
                         "entry": entry, "steps": [], "detail": "already there"})
            return {"ok": True, "app": app, "node": node_id, "steps": [],
                    "detail": "already at %s" % node_id, "state": _slim_state(st)}
        steps = []

        def _finish(ok, error=None):
            """Record the run: what it steered by, what it did, where it ended up."""
            try:
                after = _slim_state(self._gamestate(b).get("states", {}).get(app) or {})
            except Exception:
                after = None
            self._trace({"app": app, "node": node_id, "depth": _depth, "ok": ok,
                         "entry": entry, "exit": after, "steps": steps, "error": error,
                         "route": route_label})

        def fail(step, why):
            steps.append({"step": step, "ok": False, "error": why})
            _finish(False, why)
            return {"ok": False, "app": app, "node": node_id, "steps": steps, "error": why}

        route_label = None
        steps.extend(self._preflight(b, app, tree))
        # RE-READ after preflight: clearing a ghost modal can change the reported scene,
        # and planning from the pre-clear reading is planning from a state we just left.
        st = self._gamestate(b).get("states", {}).get(app) or st
        start = self._planner_state(st)
        rt = plan.route(tree, app, start, node_id, signals=st.get("signals"),
                        exclude=_exclude)
        if rt.get("ok"):
            route_label = plan.summarize(rt)
            # A RESTART is never silent. Reaching a state by killing the app discards
            # unsaved progress -- which is what quitting would discard anyway, so it is
            # defensible, but a `goto` that restarts the game because a confirm was refused
            # is a surprise, and surprises get named here. Callers who would rather fail can
            # say so.
            restarts = [e.get("id") for e in rt["route"]
                        if any("restart" in stp for stp in (e.get("steps") or []))]
            if restarts:
                why = ("after %d refused edge(s): %s" % (len(_exclude), ", ".join(sorted(_exclude)))
                       if _exclude else "it is the cheapest route")
                if no_restart:
                    err = ("the only route to %s RESTARTS %s (%s) and no_restart was set"
                           % (node_id, app, why))
                    return fail({"restart_refused": restarts}, err)
                steps.append({"step": {"restart_planned": restarts}, "ok": True,
                              "detail": "this route RESTARTS %s -- %s. Unsaved progress is "
                                        "discarded; the save file is untouched." % (app, why)})
            self.bus.publish("gamego", app=app, node=node_id, step={"plan": route_label})
            r = self._drive_route(b, app, tree, rt["route"], steps, _depth, start=start)
            if r.get("ok"):
                _finish(True)
                return {"ok": True, "app": app, "node": node_id, "steps": steps,
                        "route": route_label, "planned": True,
                        "state": _slim_state(self._gamestate(b).get("states", {}).get(app))}
            # THE RE-PLAN. A failed edge usually means the app is not where the route
            # thought — a confirm we did not expect, a screen that ignored a key. If it
            # MOVED, the honest response is to plan again from where it actually is; if it
            # did not, planning again would produce the same route and loop.
            #
            # A REFUSAL is different in kind and gets its own arm. "This edge cannot work in
            # this state" is definite, so excluding that edge id and planning again CANNOT
            # reproduce the same route — the loop the moved-guard protects against is not
            # reachable, which is precisely what makes re-planning safe here without it.
            # On a Classic save that is the difference between "goto title gives up" and
            # "goto title takes the restart route the graph already had".
            st2 = self._gamestate(b).get("states", {}).get(app) or {}
            moved = self._planner_state(st2)
            if r.get("refused") and r.get("refused_edge") and _replans < 4:
                nxt_ex = tuple(sorted(set(_exclude) | {r["refused_edge"]}))
                steps.append({"step": {"replan": True, "excluding": r["refused_edge"]}, "ok": True,
                              "detail": "%s REFUSED at %s; excluding that edge and re-planning"
                                        % (r["refused_edge"], start)})
                _finish(False, r.get("error"))
                again = self._gamego(b, app, node_id, _depth, _replans + 1,
                                     _exclude=nxt_ex, no_restart=no_restart)
                again["steps"] = steps + (again.get("steps") or [])
                return again
            if _replans < 2 and moved != start:
                steps.append({"step": {"replan": True}, "ok": True,
                              "detail": "%s failed at %s; re-planning from %s"
                                        % (route_label, start, moved)})
                _finish(False, r.get("error"))
                again = self._gamego(b, app, node_id, _depth, _replans + 1,
                                     _exclude=_exclude, no_restart=no_restart)
                again["steps"] = steps + (again.get("steps") or [])
                return again
            _finish(False, r.get("error"))
            # Surface the refusal FLAG on the result too. Callers were otherwise left
            # grepping the error text to tell "declined on purpose" from "broke", and a tour
            # script doing exactly that mis-labelled a run that refused one edge, re-planned
            # past it, and then failed on a LATER edge for an unrelated reason.
            return {"ok": False, "app": app, "node": node_id, "steps": steps,
                    "refused": bool(r.get("refused")), "route": route_label,
                    "error": r.get("error")}

        # No route in the graph — fall back to the node's legacy recipe if it still has
        # one. Reported either way: a node driven by a recipe is a node whose transitions
        # nobody has written yet, and that should be visible, not silent.
        recipe = (node.get("goto") or {}).get(app)
        if not recipe:
            _finish(False, rt.get("error"))
            return {"ok": False, "app": app, "node": node_id, "steps": steps,
                    "error": rt.get("error"), "from": start}
        route_label = "legacy recipe (%s)" % rt.get("error", "no route")
        self.bus.publish("gamego", app=app, node=node_id, step={"legacy": node_id})
        for step in recipe:
            self.bus.publish("gamego", app=app, node=node_id, step=step)
            r = self._run_step(b, app, tree, step, _depth)
            if not r.get("ok"):
                return fail(step, r.get("error", "step failed"))
            steps.append({"step": step, "ok": True, "detail": r.get("detail", "")})
        st = self._gamestate(b).get("states", {}).get(app)
        # A recipe that ran every step without error has still not necessarily ARRIVED --
        # that is the case the trace exists for, so record where it actually ended up.
        _finish(True)
        return {"ok": True, "app": app, "node": node_id, "steps": steps,
                "route": route_label, "planned": False, "state": _slim_state(st)}

    @staticmethod
    def _merge_signals(states, app):
        """One signal view across BOTH apps, the step's own first.

        The signals worth guarding a step on are properties of the PAIR, not of a window:
        `game_live` is a probe of Qud's bridge, so it is None on Raves' profile (no port) and a
        raves step asking for it would be guarding on a value that is structurally always unknown.
        """
        merged = {}
        for src in [app] + [a for a in (states or {}) if a != app]:
            for k, v in ((states.get(src) or {}).get("signals") or {}).items():
                if merged.get(k) is None and v is not None:
                    merged[k] = v
        return merged

    @staticmethod
    def _step_requires_unmet(requires, signals):
        """Which of a STEP's `requires` do not hold -> {} when it may run.

        STRICTER THAN THE PLANNER'S COPY, deliberately. plan.py::_requires_hold lets an UNKNOWN
        signal pass, which is right when choosing a route: refusing to plan because we did not
        poll something is worse than trying an edge that verifies its own arrival. Here the
        guarded step IS the action, and the actions worth guarding are the ones you cannot take
        back -- "press select on the save picker" must not fire because we could not tell whether
        a game was live. Unknown means DON'T.
        """
        out = {}
        for k, want in (requires or {}).items():
            have = (signals or {}).get(k)
            if have is None or bool(have) != bool(want):
                out[k] = have
        return out

    def _run_step(self, b, app, tree, step, _depth=0):
        """Execute ONE step of a transition or a legacy recipe -> {ok, detail?, error?}.

        One vocabulary, shared by both drivers on purpose: the graph refactor changed how
        routes are CHOSEN, not what a move is. Every hard-won step — the hover-first click,
        the conditional dismiss that verifies what it pressed, the first-party bridge exit
        for Qud's modern menus — carries over unchanged.
        """
        import os as _os
        import time
        from . import gametree

        # PER-OS STEPS. The same screen sometimes has to be reached differently on each
        # platform, and the two ways are not a disagreement to settle -- they are both
        # right, on their own machine. The 2026-08-08 merge produced seven of these at
        # once: `click_text` needs OCR, which only `backends/darwin.py` implements, so on
        # Windows those edges died with `ocr failed:` and took the chargen subtree with
        # them. Overwriting them with coordinates would have re-imported the failure the
        # Mac's "click by LABEL, not coords" rule exists to prevent (stray games, twice).
        #
        # So an edge may carry BOTH forms, each tagged with the `os.name` it belongs to,
        # and each machine skips the other's. `tools/selftest_plan.py` then enforces the
        # thing that makes this safe: an edge must keep at least one ACTUATING step under
        # every os, because a step list that skips itself empty is an edge that reports
        # OK while doing nothing -- the failure family this codebase keeps rediscovering.
        if "os" in step and step["os"] != _os.name:
            return {"ok": True, "detail": "skipped (os=%s, here=%s)" % (step["os"], _os.name)}

        # PER-STEP `requires`, evaluated against the LIVE signals — the same block the planner
        # already applies to a whole EDGE (plan.py::_requires_hold), now usable on one step of
        # one. An edge sometimes has to do different things depending on state it cannot know
        # until it gets there: whether a game is live decides whether Raves' load picker should
        # be confirmed or backed out of, and that is one edge with two arms, not two edges.
        #
        # This block is why the feature had to be REAL rather than assumed: `requires` on a step
        # was silently ignored before, so a step written with one would run unconditionally while
        # reading, to anyone maintaining the tree, as if it were guarded. That is the same
        # skips-itself-and-reports-ok family the `os` note above exists for, only worse — it does
        # the thing rather than nothing. An UNKNOWN signal (None) does NOT satisfy a requirement:
        # if we cannot tell, we do not fire a guarded step.
        if "requires" in step:
            # STRICTER THAN THE PLANNER'S COPY, and deliberately. plan.py::_requires_hold lets an
            # UNKNOWN signal pass, which is right when choosing a route -- refusing to plan
            # because we did not poll something is worse than trying an edge that verifies its own
            # arrival. Here the guarded step is the ACTION, and the actions worth guarding are the
            # ones you cannot take back: "press select on the save picker" must not fire because
            # we could not tell whether a game was live. Unknown means DON'T.
            #
            # Signals are resolved ACROSS apps, because the interesting ones are properties of the
            # pair rather than of a window: `game_live` is a probe of Qud's bridge, so it is None
            # on Raves' profile (no port) and a raves edge asking for it would otherwise be
            # guarding on a value that is structurally always unknown.
            states = (self._gamestate(b).get("states") or {})
            merged = Engine._merge_signals(states, app)
            unmet = Engine._step_requires_unmet(step["requires"], merged)
            if unmet:
                return {"ok": True, "detail": "skipped (requires %s, have %s)"
                        % (step["requires"], unmet)}

        try:
            if "goto" in step:
                # A LEGACY-RECIPE step: run another node's recipe as a prefix. The graph
                # does not need it (an edge names its own source), and it survives only to
                # keep unmigrated recipes working.
                #
                # "unless_within": skip the chain when we are already INSIDE the node it names.
                # Every status TAB chained through `status_screens`, whose own recipe starts at
                # `in_game` -- unreachable from inside the status screens without closing them --
                # so tab-to-tab switching failed while switching from the map worked. OPT-IN,
                # because plenty of recipes chain to an ancestor precisely to get back to a known
                # base: making the skip automatic fixed Qud and broke Raves. In the graph this
                # whole distinction disappears -- the planner inserts the exit edge only when the
                # route actually needs it.
                if step.get("unless_within"):
                    cur = self._gamestate(b).get("states", {}).get(app) or {}
                    if step["goto"] in (cur.get("path") or []):
                        return {"ok": True, "detail": "already within"}
                r = self._gamego(b, app, step["goto"], _depth + 1)
                return _step_ok(r.get("ok"), r.get("detail"), r.get("error") or "goto failed")

            if "launch" in step:
                cfg = gametree.apps(tree).get(app, {})
                if step.get("unless_running") and self._find_win(b.list_targets(), cfg.get("window")):
                    return {"ok": True, "detail": "already running"}
                from .launch import resolve_launch
                try:
                    spec, largs = resolve_launch(step["launch"])
                except KeyError as e:
                    return {"ok": False, "error": str(e.args[0] if e.args else e)}
                if not spec:
                    return {"ok": False, "error": "no launcher %r" % step["launch"]}
                r = b.launch(spec, largs)
                return _step_ok(r.ok, r.detail, r.error or "launch failed")

            if "restart" in step:
                # The last-resort edge: kills every instance (duplicates included), relaunches,
                # and waits for the app to REPORT rather than merely to show a window. Priced at
                # 120 in the cost table so the planner reaches for it only when nothing else can
                # get out of where we are.
                r = self._restart_app(b, step["restart"])
                return _step_ok(r.get("ok"), "restarted %s" % step["restart"],
                                r.get("error") or "restart failed")

            if "wait_window" in step:
                deadline = time.monotonic() + float(step.get("timeout", 30))
                while self._find_win(b.list_targets(), step["wait_window"]) is None:
                    if time.monotonic() > deadline:
                        return {"ok": False,
                                "error": "window %r never appeared" % step["wait_window"]}
                    time.sleep(1.0)
                return {"ok": True}

            if "activate" in step:
                win = self._find_win(b.list_targets(), step["activate"])
                if win is None:
                    return {"ok": False, "error": "no window %r" % step["activate"]}
                r = b.activate(win.id)
                time.sleep(0.6)
                return _step_ok(r.ok, None, r.error or "activate failed")

            if "hover" in step:
                # A hover is NOT a weak click. Qud's chargen carousel selects the card
                # under the cursor and confirms the current selection on a click that
                # lands anywhere else, so "put the cursor on card B" and "press it" are
                # separate verbs; an edge with only clicks cannot say the first, and
                # ends up confirming whatever happened to be selected.
                win = self._find_win(b.list_targets(), step.get("window", ""))
                if win is None:
                    return {"ok": False, "error": "no window %r" % step.get("window")}
                x, y = step["hover"]
                r = b.mouse_move(win.id, int(x), int(y))
                if not r.ok:
                    return {"ok": False, "error": r.error or "hover failed"}
                time.sleep(0.35)
                return {"ok": True}

            if "click_hover" in step or "click" in step:
                key = "click_hover" if "click_hover" in step else "click"
                win = self._find_win(b.list_targets(), step.get("window", ""))
                if win is None:
                    return {"ok": False, "error": "no window %r" % step.get("window")}
                x, y = step[key]
                kw = {"hover": True} if key == "click_hover" else {}
                r = b.click(win.id, int(x), int(y), **kw)
                if not r.ok:
                    return {"ok": False, "error": r.error or "click failed"}
                time.sleep(0.5)
                return {"ok": True}

            if "click_text" in step:
                win = self._find_win(b.list_targets(), step.get("window", ""))
                if win is None:
                    return {"ok": False, "error": "no window %r" % step.get("window")}
                want = str(step["click_text"]).strip().lower()
                # POLL, don't snapshot. Qud's window does not repaint while unfocused, so the
                # first frame after an `activate` is whatever was on screen BEFORE — the
                # documented settle is ~2s and `activate` waits 0.6. A single OCR therefore
                # read the old screen and failed to find a label that was plainly there:
                # `goto qud in_game` from the title reported "text 'continue' not on screen
                # (14 ocr lines)" while looking straight at Continue. The 14 lines were the
                # in-game HUD it had just left.
                #
                # Retrying also covers the honest second case -- a menu still animating in --
                # and costs nothing when the text is already up, which is the normal path.
                # Deliberately NOT fixed by sleeping longer after activate: that taxes every
                # step to serve the one that reads pixels.
                deadline = time.monotonic() + float(step.get("timeout", 6.0))
                hit, boxes, tries = None, [], 0
                while True:
                    tries += 1
                    try:
                        ocr = b.ocr(win.id)
                    except Exception as e:
                        return {"ok": False, "error": "ocr failed: %s" % e}
                    boxes = ocr.get("boxes") or []
                    hit = _ocr_find(boxes, want)
                    if hit is not None or time.monotonic() >= deadline:
                        break
                    time.sleep(0.5)
                if hit is None:
                    return {"ok": False,
                            "error": "text %r not on screen after %d OCR passes (%d lines: %s)"
                            % (want, tries, len(boxes),
                               ", ".join(x.get("text", "") for x in boxes[:8]))}
                bx, by, bw, bh = hit["bbox"]
                # ocr bbox is in CAPTURE px; clicks are window points (Retina shot = 2x)
                scale = (float(ocr.get("w") or win.w) / float(win.w)) if win.w else 1.0
                cx, cy = int((bx + bw / 2.0) / scale), int((by + bh / 2.0) / scale)
                # optional [dx,dy] when the hit-area sits away from the label (Qud's
                # Back chevron lives ~40px above its "[Esc] Back" caption)
                ox, oy = step.get("offset") or (0, 0)
                cx, cy = cx + int(ox), cy + int(oy)
                r = b.click(win.id, cx, cy, hover=True)
                if not r.ok:
                    return {"ok": False, "error": r.error or "click failed"}
                time.sleep(0.5)
                return {"ok": True, "detail": "%r @ win(%d,%d)" % (hit["text"], cx, cy)}

            if "key" in step:
                win = self._find_win(b.list_targets(), step.get("window", ""))
                if win is None:
                    return {"ok": False, "error": "no window %r" % step.get("window")}
                r = b.key(win.id, step["key"], focus=True)
                time.sleep(0.4)
                return _step_ok(r.ok, None, r.error or "key failed")

            if "sleep" in step:
                time.sleep(float(step["sleep"]))
                return {"ok": True}

            if "bridge" in step:
                # first-party command over the Qud mod bridge (e.g. "uiback", "statustab")
                r = self._qud_bridge(step["bridge"], args=step.get("args"))
                if not r.get("ok"):
                    return {"ok": False, "error": r.get("error", "bridge send failed")}
                time.sleep(0.6)
                return {"ok": True, "detail": step["bridge"]}

            if "command" in step:
                # A named QUD command through the mod (CmdQuit &c) plus the confirms it
                # raises, answered BY BUTTON. Binding-independent, and the only way to start a
                # flow the game's own UI owns -- Qud's modern screens ignore synthesized keys.
                # Unconditional here, because a transition already knows which state it is
                # leaving; the `dismiss` form below is for when that has to be re-checked.
                #
                # The answers are delivered ON ARRIVAL over a held bridge connection, not on a
                # 1.2s timer. The timer version could not fail -- a socket write succeeds
                # whether or not a modal is up -- so an unanswered prompt surfaced only as the
                # verify timing out 45s later, which is how a Classic save's third, TEXT-INPUT
                # confirm got mistaken three times for the process ageing. See
                # `_qud_command_chain`.
                answers = step.get("answers") or []
                r = self._qud_command_chain(step["command"], answers,
                                            timeout=float(step.get("answer_timeout", 25)))
                if not r.get("ok"):
                    return {"ok": False, "refused": bool(r.get("refused")),
                            "error": "command %s: %s" % (step["command"], r.get("error"))}
                return {"ok": True,
                        "detail": "%s + %d answer(s)" % (step["command"],
                                                         len(r.get("answered") or []))}

            if "load_save" in step:
                # Load a save through the mod's own `loadsave {id}` bridge command: exact ID,
                # no coordinates, no OCR, and it opens Qud's picker itself from the title.
                #
                # This replaced a blind {"click_hover": [900, 190]} "top save row" that the
                # recipe had carried for months and that failed here on the first live run of
                # the planner -- the same top-row roulette `hv loadsave` was written to end,
                # still hiding inside the one recipe nobody had re-driven. Take the value as a
                # NAME, or {"row": n} to resolve it from DISK metadata (which is what makes it
                # generic: the graph should not hardcode a character).
                sel = step["load_save"]
                name = sel
                if not isinstance(sel, str):
                    row = int((sel or {}).get("row", 0))
                    saves = self._qud_saves()
                    if not saves.get("ok"):
                        return _step_ok(False, None, saves.get("error", "save list failed"))
                    match = next((s for s in saves.get("saves") or [] if s.get("row") == row), None)
                    if match is None:
                        return _step_ok(False, None, "no save at row %d (%d saves)"
                                        % (row, len(saves.get("saves") or [])))
                    name = match["name"]
                r = self._load_save(b, name)
                return _step_ok(r.get("ok"), "loaded %r via %s" % (name, r.get("via", "")),
                                r.get("error") or "load failed")

            if "dock" in step:
                # place the window by its standing dock rule (Raves stacks above Qud with the
                # anchor's size) -- a fresh solo launch otherwise lands wherever the OS puts it
                # and window-relative clicks assume the standard 1920x1080 slot.
                r = self._dock(b, step["dock"])
                time.sleep(0.4)
                return _step_ok(r.get("ok"), None, r.get("error") or "dock failed")

            if "dismiss" in step:
                return self._run_dismiss(b, app, tree, step)

            if "assert" in step:
                a = dict(step["assert"])
                a.setdefault("app", app)
                a["timeout"] = step.get("timeout", a.get("timeout", 15))
                r = self._assert_state(b, a)
                if not r.get("passed") and step.get("optional"):
                    # A SOFT WAIT: block until the condition holds, but do not make the edge
                    # depend on it. For "wait until the app is ready" the condition is a
                    # better sleep, not a precondition -- the edge must still work in the
                    # case where it legitimately never becomes true.
                    return _step_ok(True, "waited for %s (not met; continuing)" % (a,))
                if not r.get("passed"):
                    act = r.get("actual") or {}
                    # NAME the modal. The label alone reads "In-Game" for every stage of a
                    # quit chain, so an assert that failed because a prompt was still up
                    # said nothing about which prompt -- the same silence this whole edge
                    # has been debugged through twice.
                    up = ((act.get("extra") or {}).get("popup")) or ""
                    return {"ok": False, "error": "assert failed: wanted %s, got %s%s"
                            % (a, act.get("label"), (" with a %r modal up" % up) if up else "")}
                return {"ok": True}

            if set(step) <= {"note"}:
                return {"ok": True, "detail": "note only"}
            return {"ok": False, "error": "unknown step %r" % step}
        except Exception as e:
            # A step that RAISES must fail its transition, not the whole daemon thread --
            # the route is mid-flight and the trace is what tells us where it stopped.
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    def _run_dismiss(self, b, app, tree, step):
        """A conditional self-heal step: if the app is in the named state, clear it.

        Kept verbatim from the recipe model, and still earning its place inside
        transitions. The confirms raised by leaving a live game all share one scene, so
        only the reported POPUP tells them apart -- and a blind Space on a screen with no
        confirm up does something else entirely. Every branch VERIFIES that what it pressed
        actually moved something: a silent "dismissed" is how a stray Cancel once reached
        the main menu, where Cancel means quit.
        """
        import time
        from . import gametree
        cond = step["dismiss"]
        cur = (self._gamestate(b).get("states", {}).get(app) or {})
        scene = (cur.get("signals") or {}).get("scene") or ""
        # `popup` conditions on the app's reported MODAL kind rather than its screen.
        # Leaving a live game means answering a chain of confirms while the scene stays
        # "in_game" throughout, so scene alone cannot tell the steps apart.
        want_popup = cond.get("popup")
        have_popup = str(((cur.get("extra") or {}).get("popup")) or "")
        if want_popup is not None:
            hit = self._popup_matches(have_popup, want_popup)
        else:
            # A LIST of scenes is one condition, not several steps: Raves reports its status
            # screens as eight distinct scenes that all clear the same way, and spelling out
            # eight dismiss steps would run eight state polls to clear one screen.
            want_scene = cond.get("scene", "")
            want_list = want_scene if isinstance(want_scene, list) else [want_scene]
            hit = str(scene).lower() in [str(w).lower() for w in want_list]
        # What the step must CHANGE. Taking the PAIR, rather than the scene, is what lets one
        # branch serve all three shapes: closing a screen moves the scene, raising a confirm
        # moves the popup, and answering one moves the popup back -- CmdQuit does the middle,
        # and demanding a scene change failed it even though it had worked.
        before = self._dismiss_fingerprint(cur, scene)
        cfg = gametree.apps(tree).get(app, {})

        # A REFUSAL step: "if this is up, we cannot go on -- clear it and say why."
        # An answer chain is a fixed length, so the thing it cannot express is a prompt it
        # did not plan for; without a trailing refusal that prompt is simply left standing
        # and the edge dies at its verify, blaming nothing. Cancelling (rather than only
        # reporting) is what keeps the failure from poisoning the next attempt -- a modal
        # left up eats every later keystroke and makes the NEXT run fail for a different,
        # wrong-looking reason.
        if hit and cond.get("refuse"):
            win0 = self._find_win(b.list_targets(), cfg.get("window"))
            cancelled = False
            if win0 is not None:
                try:
                    b.key(win0.id, cond.get("key", "Escape"), focus=True)
                    time.sleep(0.8)
                    cancelled = True
                except Exception:
                    cancelled = False
            return {"ok": False, "refused": True,
                    "error": "refusing to continue: a %r modal is up -- %s. %s"
                             % (have_popup or scene, cond["refuse"],
                                "Cancelled it; the app is left clean." if cancelled
                                else "Could not reach the window to cancel it.")}

        if not hit:
            # "Not the modal I name" is NOT the same as "no modal", and conflating them is
            # how a step that cannot fail gets written. `{"popup": "message"}` matched none
            # of Qud's ABANDON confirm (an AskString, mirrored by Raves as `input`), so all
            # four steps of `raves in_game -> title` reported ok while the prompt sat there
            # and the route died 25s later at its verify with nothing naming the cause --
            # the same shape as the Qud-side bug this pattern already produced once.
            #
            # A condition that wants "whatever is in the way" should say `popup: true`; one
            # that names a kind is claiming to answer THAT prompt, so another modal being up
            # is a real error and is reported as one.
            if want_popup is not None and have_popup:
                detail = ("dismiss expects popup %r but a %r modal is up"
                          % (want_popup, have_popup))
                if str(have_popup).lower() == "input":
                    # A TEXT-INPUT modal. Never send this step's keys at it: `space` types a
                    # space into the box and `Right` moves the caret, so an answer chain aimed
                    # at a button confirm silently edits someone's text field. For the quit
                    # chain that field is Qud's "type ABANDON to confirm", whose completion
                    # ends a permadeath run -- refuse it, cancel it so the app is left clean
                    # and not poisoned for the next attempt, and say why.
                    win0 = self._find_win(b.list_targets(), cfg.get("window"))
                    cancelled = False
                    if win0 is not None:
                        try:
                            b.key(win0.id, "Escape", focus=True)
                            time.sleep(0.8)
                            cancelled = True
                        except Exception:
                            cancelled = False
                    return {"ok": False,
                            "error": "%s. That is a text-input confirmation this step must "
                                     "not type into -- Qud asks one ('type ABANDON to "
                                     "confirm') when the save has no checkpointing, i.e. a "
                                     "Classic character, and completing it quits WITHOUT "
                                     "SAVING and ends a permadeath run. %s"
                                     % (detail,
                                        "Cancelled it; the game is still live."
                                        if cancelled else
                                        "Could not reach the window to cancel it.")}
                return {"ok": False,
                        "error": "%s -- refusing to press this step's affordance at a modal "
                                 "it was not written for. Use `popup: true` if the step is "
                                 "meant to clear whatever is up." % detail}
            return {"ok": True, "detail": "not present"}
        win = self._find_win(b.list_targets(), cfg.get("window"))
        if win is None:
            return {"ok": False, "error": "dismiss: no window for %s" % app}
        if cond.get("command"):
            # Same observed-arrival delivery as the `command` step above -- a dismiss that
            # answers a chain must be able to report an unanswered prompt, not just that its
            # writes left the machine. With no `answers` this is a plain command send.
            r = self._qud_command_chain(cond["command"], cond.get("answers") or [],
                                        timeout=float(cond.get("answer_timeout", 25)))
            if not r.get("ok"):
                return {"ok": False, "refused": bool(r.get("refused")),
                        "error": "dismiss command: %s" % r.get("error")}
        elif cond.get("bridge"):
            # first-party dismissal -- no OCR, no coords, no focus steal
            r = self._qud_bridge(cond["bridge"])
            if not r.get("ok"):
                return {"ok": False, "error": "dismiss bridge: %s" % r.get("error")}
        elif cond.get("click_text"):
            # Qud's modern UI screens IGNORE OS-synthesized keys (the GameSummaryScreen
            # gotcha generalizes) -- but synthesized clicks land, so exit via the screen's
            # clickable affordance. A miss here MUST fail: a fuzzy match that clicks the
            # wrong thing on the wrong screen is how stray games get started.
            want = str(cond["click_text"]).strip().lower()
            try:
                ocr = b.ocr(win.id)
            except Exception as e:
                return {"ok": False, "error": "dismiss ocr failed: %s" % e}
            boxes = ocr.get("boxes") or []
            found = _ocr_find(boxes, want)
            if found is None:
                return {"ok": False, "error": "dismiss: text %r not on the %s screen"
                        % (cond["click_text"], scene)}
            bx, by, bw, bh = found["bbox"]
            sc = (float(ocr.get("w") or win.w) / float(win.w)) if win.w else 1.0
            ox, oy = cond.get("offset") or (0, 0)
            b.click(win.id, int((bx + bw / 2.0) / sc) + int(ox),
                    int((by + bh / 2.0) / sc) + int(oy), hover=True)
        elif cond.get("answers"):
            # A fixed chain of popup ANSWERS, first-party through the mod. Qud reports no
            # popup state of its own, so the steps cannot be conditioned individually the way
            # Raves' can -- but an answer with nothing to answer is a no-op, so the chain is
            # safe to send blind and still says exactly what it is doing.
            for btn in cond["answers"]:
                rr = self._qud_popup_answer(btn)
                if not rr.get("ok"):
                    return {"ok": False, "error": "dismiss answer: %s" % rr.get("error")}
                time.sleep(1.2)
        elif cond.get("keys"):
            # A SEQUENCE, verified once at the end. Needed because some keys move a selection
            # INSIDE a modal rather than answering it: the Right that shifts a confirm from
            # Yes to No changes nothing observable, so verifying after it fails a step that
            # worked.
            for k in cond["keys"]:
                b.key(win.id, k, focus=True)
                time.sleep(0.5)
        else:
            b.key(win.id, cond.get("key", "Escape"), focus=True)
        # verify the dismissal actually TOOK -- one that did not must stop the run, or later
        # steps land on the wrong screen. For a popup condition the thing that must change is
        # the modal, not the scene: answering a quit confirm leaves the scene where it was.
        what = ("popup %s" % want_popup) if want_popup is not None else scene
        for _ in range(8):
            time.sleep(0.7)
            cur2 = (self._gamestate(b).get("states", {}).get(app) or {})
            now = self._dismiss_fingerprint(cur2)
            if now != before:
                return {"ok": True, "detail": "dismissed %s" % what}
        return {"ok": False, "error": "dismiss ran but %s is still up" % what}

    def _apply_layout(self, b, name):
        from .layouts import load_layouts, placement_rect
        lay = load_layouts().get(name)
        if not lay:
            return {"ok": False, "error": "no layout %r" % name}
        sw, sh = b.screen_size()
        try:
            displays = b.displays()   # for "monitor" placements; optional per backend
        except Exception:
            displays = []
        wins = b.list_targets()
        used = set()
        results = []
        for pl in lay.get("placements", []):
            m = (pl.get("match") or "").lower()
            win = None
            for t in wins:
                if t.id in used:
                    continue
                if (not m or m in (t.title or "").lower()
                        or m in (t.class_name or "").lower()):
                    win = t
                    break
            if win is None:
                results.append({"match": pl.get("match"), "ok": False,
                                "error": "no matching window"})
                continue
            used.add(win.id)
            try:
                x, y, w, h = placement_rect(pl, sw, sh, displays)
            except Exception as e:
                results.append({"match": pl.get("match"), "target": win.id,
                                "ok": False, "error": str(e)})
                continue
            r = self._place(b, win.id, win.title, x, y, w, h, pl.get("topmost"))
            results.append({"match": pl.get("match"), "target": win.id,
                            "title": win.title, "ok": r.ok,
                            "rect": [x, y, w, h], "error": r.error})
        applied = sum(1 for x in results if x["ok"])
        if applied > 0:
            # Remember it as the standing stage, so a restart can put the windows back
            # without anyone having to remember to re-run this by hand.
            from .layouts import remember_layout
            remember_layout(name)
        return {"ok": applied > 0, "applied": applied, "results": results,
                "detail": "%s: %d/%d placed" % (name, applied, len(results))}

    def stop(self):
        job = _Job(None)
        self._q.put(job)
        job.event.wait(timeout=2.0)
