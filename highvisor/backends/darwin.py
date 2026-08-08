"""MacBackend — observe + control via the Accessibility API (AX) and CoreGraphics.

Mirrors WindowsBackend behind the same PlatformBackend seam (docs/01-architecture):
  - ``CGWindowListCopyWindowInfo`` enumerates on-screen windows; the target id is
    the CGWindowNumber (``win:<n>``).
  - ``CGWindowListCreateImage`` captures a SPECIFIC window even when it is
    unfocused/occluded — the macOS analogue of PrintWindow (tier-3 observation).
  - ``AXUIElement`` actions (``AXSetValue`` / ``AXPress`` / set ``AXPosition``)
    act on a background window WITHOUT bringing it frontmost — the reliable
    background path (tier 1). Hammerspoon's hs.axuielement proves this.
  - ``CGEventPostToPid`` delivers a keystroke to a pid (tier 2); ``activate`` +
    ``CGEventPost`` is the focus-stealing last resort (tier 4).

Coordinates: everything here is in **points** (the top-left-origin global display
coordinate space that AX and CGWindow bounds use) — uniform across displays and
what window positioning needs, so no per-monitor scale juggling. Only the captured
PNG is in native pixels (its own resolution). NB: on the Windows backend the unit
is physical pixels; a client that mixes the two across OSes must account for that.

TCC: AX needs the *Accessibility* grant (``AXIsProcessTrusted``); window capture
needs *Screen Recording*. We detect and raise a precise BackendError rather than
failing opaque (docs/03 risk table).
"""
import time
from io import BytesIO
from typing import List, Optional

import Quartz
from AppKit import NSBitmapImageRep, NSRunningApplication, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted, AXIsProcessTrustedWithOptions, AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
    AXUIElementPerformAction, AXUIElementSetAttributeValue, AXValueCreate,
    AXValueGetValue)
try:  # the CGPoint/CGSize AXValue-type constants were renamed across pyobjc versions
    from ApplicationServices import kAXValueCGPointType, kAXValueCGSizeType
except ImportError:  # newer pyobjc
    from ApplicationServices import (
        kAXValueTypeCGPoint as kAXValueCGPointType,
        kAXValueTypeCGSize as kAXValueCGSizeType)

from ..apps import PROFILES
from ..backend import ActionResult, BackendError, Element, PlatformBackend, Target

# NSBitmapImageFileTypePNG is 4; the symbol moved across pyobjc versions, so pin it.
_PNG = 4

_CAPTURE_DENIED = ("capture returned nothing — is Screen Recording granted? "
                   "(System Settings > Privacy & Security > Screen Recording)")

# AX attribute / action names as plain strings — robust across pyobjc versions.
_ROLE, _TITLE, _DESC = "AXRole", "AXTitle", "AXDescription"
_VALUE, _POS, _SIZE = "AXValue", "AXPosition", "AXSize"
_CHILDREN, _WINDOWS = "AXChildren", "AXWindows"
_FOCUSED_ELEM = "AXFocusedUIElement"
_PRESS, _RAISE = "AXPress", "AXRaise"
_EDITABLE_ROLES = ("AXTextArea", "AXTextField", "AXComboBox")

# key name -> macOS virtual keycode (for CGEventCreateKeyboardEvent).
KEYCODE = {
    "RETURN": 0x24, "ENTER": 0x24, "TAB": 0x30, "SPACE": 0x31,
    "DELETE": 0x33, "BACKSPACE": 0x33, "BACK": 0x33,
    "ESC": 0x35, "ESCAPE": 0x35, "FORWARDDELETE": 0x75, "DEL": 0x75,
    "LEFT": 0x7B, "RIGHT": 0x7C, "DOWN": 0x7D, "UP": 0x7E,
    "HOME": 0x73, "END": 0x77, "PAGEUP": 0x74, "PAGEDOWN": 0x79,
    "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61,
    "F7": 0x62, "F8": 0x64, "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F,
}


def _running_image() -> str:
    """The executable image this process is actually running as — NOT sys.executable. The
    framework python re-execs into Python.app, so the two differ, and only this one is what
    TCC checks for Accessibility."""
    import ctypes
    import ctypes.util
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        size = ctypes.c_uint32(4096)
        buf = ctypes.create_string_buffer(size.value)
        if libc._NSGetExecutablePath(buf, ctypes.byref(size)) == 0:
            import os
            return os.path.realpath(buf.value.decode())
    except Exception:
        pass
    import sys
    return sys.executable


def _split_mods(spec):
    """Modifier list, comma- OR plus-separated. The two platform lines grew different
    spellings — `cmd,ctrl` on mac, `ctrl+alt+shift` on Windows — and unifying the name
    without unifying the SEPARATOR would just move the bug."""
    return [m.strip().lower() for m in str(spec or "").replace("+", ",").split(",") if m.strip()]


class MacBackend(PlatformBackend):
    name = "macos"

    def thread_init(self):
        pass  # no COM apartment analogue; AX/CG are fine on our single worker thread

    def _require_ax(self):
        if not AXIsProcessTrusted():
            raise BackendError(
                "Accessibility permission is not granted to this process. Grant it in "
                "System Settings > Privacy & Security > Accessibility (add the terminal / "
                "python running highvisor), then retry.")

    def request_input_grant(self) -> dict:
        """Ask macOS to show the Accessibility prompt for THIS process, and report who it is.

        Needed because the two TCC grants resolve to DIFFERENT identities, which is not
        obvious and cost an evening:

          * Screen Recording resolves via the RESPONSIBLE process — the launchd job, i.e.
            Highvisor.app. Granting the bundle works.
          * Accessibility is keyed to the calling process's own signed identity. The
            framework build's `bin/python3.9` re-execs into `Resources/Python.app`, so the
            daemon really is running as `com.apple.python3` no matter what launched it, and
            a grant on Highvisor.app has no effect on input at all.

        So the honest thing is to let the SYSTEM name the identity it wants, by prompting.
        The dialog also creates the correct entry in the Accessibility list, which otherwise
        may not be there to tick.

        Narrower than what highvisor had before today, for what it is worth: the grant used
        to come from Terminal, which covers every command anyone runs there.
        """
        import os
        opts = {"AXTrustedCheckOptionPrompt": True}
        trusted = bool(AXIsProcessTrustedWithOptions(opts))
        return {"ok": True, "trusted": trusted,
                "process": os.path.realpath("/proc/self/exe") if os.path.exists("/proc/self/exe")
                           else _running_image(),
                "detail": ("already trusted" if trusted else
                           "a system dialog was raised — approve it, or tick the entry it "
                           "added under Privacy & Security > Accessibility")}

    # -------------------------------------------------------- window enumeration
    def _windows(self):
        """On-screen normal app windows (layer 0), front-to-back. Raw CGWindow dicts."""
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        info = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
        out = []
        for w in info:
            if int(w.get("kCGWindowLayer", 0)) != 0:      # 0 == the normal window layer
                continue
            if not w.get("kCGWindowBounds"):
                continue
            out.append(w)
        return out

    def _resolve(self, ref: Optional[str]):
        """None/"screen" -> None. Else the raw CGWindow dict for the target.
        Accepts "win:<CGWindowNumber>", "pid:<n>" (that pid's frontmost window), or
        a case-insensitive title/owner substring."""
        if ref is None or ref == "screen":
            return None
        wins = self._windows()
        if ref.startswith("win:"):
            wid = int(ref.split(":", 1)[1])
            for w in wins:
                if int(w.get("kCGWindowNumber", -1)) == wid:
                    return w
            raise BackendError("no window with id %d" % wid)
        if ref.startswith("pid:"):
            pid = int(ref.split(":", 1)[1])
            for w in wins:                                # _windows is front-to-back
                if int(w.get("kCGWindowOwnerPID", -1)) == pid:
                    return w
            raise BackendError("no on-screen window for pid %d" % pid)
        # A KNOWN APP ALIAS ("qud", "raves") resolves through the app registry, not by raw
        # substring. This is not a convenience -- it is a correctness fix. Raves' window is
        # titled "Raves of Qud (DEBUG)", so a bare "qud" substring matched BOTH apps and the
        # front-to-back scan silently returned whichever happened to be frontmost. Every
        # `hv shot qud` taken with Raves in front captured RAVES, and the same went for
        # key/click/activate -- a wrong-window capture that still looks plausible is the
        # worst possible failure for parity work, because it scores as a perfect match.
        prof = PROFILES.get(ref.lower())
        if prof and prof.get("window"):
            want = prof["window"].lower()
            for w in wins:
                if want in (w.get("kCGWindowName") or "").lower():
                    return w
            raise BackendError("no window for app %r (expected title ~ %r)" % (ref, prof["window"]))

        low = ref.lower()
        hits = [w for w in wins
                if low in (w.get("kCGWindowName") or "").lower()
                or low in (w.get("kCGWindowOwnerName") or "").lower()]
        if not hits:
            raise BackendError("no window matching ~ %r" % ref)
        # Ambiguity is an ERROR, not a coin flip on z-order: picking the frontmost is exactly
        # how the bug above went unnoticed. Two windows of the SAME app are fine (that's one
        # target); two different apps are not.
        pids = {int(w.get("kCGWindowOwnerPID", -1)) for w in hits}
        if len(pids) > 1:
            names = ", ".join("win:%s %s" % (w.get("kCGWindowNumber"), w.get("kCGWindowName") or "?")
                              for w in hits)
            raise BackendError("%r is ambiguous across %d apps: %s -- use win:<id> or an app alias"
                               % (ref, len(pids), names))
        return hits[0]

    def _bounds(self, w) -> tuple:
        """(x, y, w, h) of a CGWindow dict, in points."""
        b = w["kCGWindowBounds"]
        return (int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"]))

    # -------------------------------------------------------------- AX plumbing
    def _ax_app(self, pid: int):
        return AXUIElementCreateApplication(pid)

    def _ax_get(self, el, attr):
        if el is None:
            return None
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        return val if err == 0 else None

    def _ax_window(self, w):
        """The AXWindow element for a CGWindow dict: the app window whose title
        matches, else its main/first window. Needs the Accessibility grant."""
        self._require_ax()
        pid = int(w["kCGWindowOwnerPID"])
        app = self._ax_app(pid)
        windows = self._ax_get(app, _WINDOWS) or []
        want = (w.get("kCGWindowName") or "").strip()
        if want:
            for ax in windows:
                if (self._ax_get(ax, _TITLE) or "").strip() == want:
                    return ax, pid
        return (windows[0] if windows else app), pid

    @staticmethod
    def _unwrap_axvalue(v, point: bool) -> tuple:
        """Pull (x, y) out of an AXPosition or (w, h) out of an AXSize AXValue box.
        Returns (0, 0) when v is None or the extraction fails."""
        if v is None:
            return (0, 0)
        if point:
            ok, pt = AXValueGetValue(v, kAXValueCGPointType, None)
            return (int(pt.x), int(pt.y)) if ok else (0, 0)
        ok, sz = AXValueGetValue(v, kAXValueCGSizeType, None)
        return (int(sz.width), int(sz.height)) if ok else (0, 0)

    def _find_editable(self, el, depth=8):
        """DFS for a text/editable descendant (for text delivery)."""
        if el is None or depth < 0:
            return None
        role = self._ax_get(el, _ROLE)
        if role in _EDITABLE_ROLES:
            return el
        for k in (self._ax_get(el, _CHILDREN) or []):
            hit = self._find_editable(k, depth - 1)
            if hit is not None:
                return hit
        return None

    # ----------------------------------------------------------------- observe
    def list_targets(self) -> List[Target]:
        front_pid = -1
        fa = NSWorkspace.sharedWorkspace().frontmostApplication()
        if fa is not None:
            front_pid = int(fa.processIdentifier())
        out = []
        for w in self._windows():
            pid = int(w.get("kCGWindowOwnerPID", -1))
            x, y, ww, hh = self._bounds(w)
            wid = int(w.get("kCGWindowNumber", 0))
            out.append(Target(
                id="win:%d" % wid, kind="window", pid=pid,
                title=w.get("kCGWindowName") or "",
                class_name=w.get("kCGWindowOwnerName") or "",
                x=x, y=y, w=ww, h=hh,
                focused=(pid == front_pid),
                visible=bool(w.get("kCGWindowIsOnscreen", True)),
                path=self._app_path(pid)))
        return out

    def _app_path(self, pid: int) -> str:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return ""
        url = app.bundleURL() or app.executableURL()
        return url.path() if url is not None else ""

    def launch(self, spec: str, args=None) -> ActionResult:
        import subprocess
        spec = (spec or "").strip()
        if not spec:
            return ActionResult.fail("empty launch spec")
        args = [str(a) for a in (args or [])]
        if "://" in spec:                              # URL scheme (steam://, …)
            cmd = ["open", spec]
        elif spec.startswith("/") or spec.endswith(".app"):
            # `-n` opens a fresh instance so passed argv actually take effect (an
            # already-running bundle would otherwise just get re-activated).
            cmd = ["open", "-n", spec] if args else ["open", spec]
        else:
            cmd = ["open", "-a", spec]                 # an app name
        if args:
            # Everything after --args is forwarded to the program's argv. Raves
            # reads these (`-- --launch-qud <coq> …`) to spawn Caves of Qud itself.
            cmd += ["--args"] + args
        try:
            subprocess.Popen(cmd)
        except Exception as e:
            return ActionResult.fail("launch failed: %s" % e)
        return ActionResult(ok=True, detail="open %s" % " ".join(cmd[1:]))

    def screen_size(self):
        d = Quartz.CGMainDisplayID()
        return (int(Quartz.CGDisplayPixelsWide(d)), int(Quartz.CGDisplayPixelsHigh(d)))

    def displays(self):
        """Every active display, in the global point coordinate space that
        ``move``/window bounds use (CGDisplayBounds), so a secondary monitor
        reports its real origin — e.g. a 4K stacked above the built-in comes back
        at a negative y. ``main`` marks CGMainDisplayID."""
        main = Quartz.CGMainDisplayID()
        err, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
        out = []
        for d in (ids or []):
            b = Quartz.CGDisplayBounds(d)
            out.append({
                "id": int(d),
                "x": int(b.origin.x), "y": int(b.origin.y),
                "w": int(b.size.width), "h": int(b.size.height),
                "main": bool(int(d) == int(main)),
            })
        return out

    def screenshot(self, target: Optional[str], native: bool = False) -> bytes:
        w = self._resolve(target)
        if w is None:
            img = Quartz.CGDisplayCreateImage(Quartz.CGMainDisplayID())
            if img is None:
                raise BackendError(_CAPTURE_DENIED)
            return self._png(img)

        wid = int(w["kCGWindowNumber"])
        # `native`: capture through ScreenCaptureKit (the non-deprecated engine;
        # CGWindowListCreateImage is deprecated as of macOS 14 and slated for
        # removal). SCK also captures at true backing scale — 2x on a Retina
        # display. It runs in a short-lived subprocess because SCK's async API
        # delivers only to a MAIN-thread run loop and we're on the Engine worker
        # thread (see highvisor/_sckshot.py). If it fails for any reason we fall
        # back to the in-process CG path so a shot never hard-fails.
        if native:
            png = self._sck_shot(wid, backing=True)
            if png is not None:
                return png

        # Default (and native fallback): CGWindowListCreateImage. On macOS 26 this
        # returns the window's native backing pixels regardless of the
        # BestResolution flag (measured: nominal == Best == 2x on Retina), so a
        # full-size window on a 1x display comes back 1:1 px<->pt — read coords
        # straight off it — while a Retina-display window comes back 2x.
        opts = (Quartz.kCGWindowImageBoundsIgnoreFraming
                | getattr(Quartz, "kCGWindowImageBestResolution", 0))
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
            wid, opts)
        if img is None:
            raise BackendError(_CAPTURE_DENIED)
        return self._png(img)

    def _sck_shot(self, wid: int, backing: bool) -> Optional[bytes]:
        """ScreenCaptureKit capture of one window via the _sckshot subprocess.
        Returns PNG bytes, or None to signal the caller to fall back (SCK missing,
        subprocess error, timeout, or empty output)."""
        import os
        import subprocess
        import sys
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".png", prefix="hv-sck-")
        os.close(fd)
        try:
            # Pass the daemon's own sys.path as PYTHONPATH so the child resolves
            # both ScreenCaptureKit (venv site-packages) and the highvisor package
            # regardless of how sys.executable itself resolves.
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
            proc = subprocess.run(
                [sys.executable, "-m", "highvisor._sckshot", str(wid),
                 "1" if backing else "0", path],
                capture_output=True, text=True, timeout=12, env=env)
            if proc.returncode != 0:
                return None
            with open(path, "rb") as f:
                data = f.read()
            return data or None
        except Exception:  # noqa: BLE001 — any failure means "fall back to CG"
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _png(self, cgimage) -> bytes:
        rep = NSBitmapImageRep.alloc().initWithCGImage_(cgimage)
        data = rep.representationUsingType_properties_(_PNG, None)
        return bytes(data)

    _AX_INPUT_NOTE = (
        "Accessibility permission is not granted to this process, so synthetic input goes "
        "NOWHERE. Grant it in System Settings > Privacy & Security > Accessibility (the bundle "
        "is \"Highvisor\" if the daemon was installed with `hv install-daemon`), then retry.")

    def _require_ax_input(self):
        """Refuse to pretend. CGEventPost does NOT fail when Accessibility is missing — it
        returns cleanly and the event is dropped, so click/key/scroll all reported ok:true
        while the target sat there doing nothing.

        That cost an hour: Raves' title menu ignored every click and keypress, the app was
        healthy, screenshots worked (Screen Recording is a SEPARATE grant and was in place),
        and every layer in between said success. Screen capture already fails loudly; input
        must too, or the first suspect is always the app.
        """
        if not AXIsProcessTrusted():
            raise BackendError(self._AX_INPUT_NOTE)

    def _hold_mods(self, src, mods: str):
        """Press the real modifier keys and return the keycodes to release.

        MEASURED TWICE, both times the hard way. Setting the flag on the event alone does NOT
        reach Godot — not for a wheel (the Ctrl+wheel panel never opened) and not for a click
        (a Ctrl+click on the playfield never fired the cell inspector, and Cmd+Right-click never
        opened the feedback form, while unmodified clicks worked perfectly throughout). An
        earlier comment here claimed flags-only was enough for Godot; it is not, and believing it
        cost a live-test case in the FULL run.

        Callers MUST release in a `finally` — an orphaned modifier makes every later synthetic
        key arrive modified and silently no-op, survives app restarts, and is close to
        undiagnosable from the app side.
        """
        held = self._mod_keycodes(mods)
        for kc in held:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateKeyboardEvent(src, kc, True))
        if held:
            time.sleep(0.04)
        return held

    def _release_mods(self, src, held):
        for kc in reversed(held or []):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateKeyboardEvent(src, kc, False))
        if held:
            time.sleep(0.03)
            self._clear_stuck_mods(keep=0)

    def click(self, target: str, x: int, y: int, button: str = "left",
              double: bool = False, hover: bool = False,
              modifiers: str = "") -> ActionResult:
        # THREE buttons, and the middle one is not optional decoration: Raves' Map Editor
        # hangs its per-object context menu off MIDDLE click, deliberately, because Qud's
        # own OnClick dispatches an unhandled `MiddleTile:x,y`. The CLI grew `--middle` with
        # the Windows backend only; this reduced `button` to `right == "right"`, so a middle
        # click here silently went out as a LEFT click — and the response `detail` echoes the
        # button you ASKED for, so the CLI printed "middle click" either way. Unreachable
        # feature plus invisible failure; parameterise all three instead.
        BUTTONS = {
            "left":   (Quartz.kCGMouseButtonLeft,   Quartz.kCGEventLeftMouseDown,
                       Quartz.kCGEventLeftMouseUp),
            "right":  (Quartz.kCGMouseButtonRight,  Quartz.kCGEventRightMouseDown,
                       Quartz.kCGEventRightMouseUp),
            # "Other" is Quartz's name for everything past the first two; the button NUMBER
            # (Center == 2) is what distinguishes them, and CGEventCreateMouseEvent writes it
            # into the event from this constant.
            "middle": (Quartz.kCGMouseButtonCenter, Quartz.kCGEventOtherMouseDown,
                       Quartz.kCGEventOtherMouseUp),
        }
        if button not in BUTTONS:
            return ActionResult.fail("unknown mouse button %r (left|right|middle)" % button)
        b, down, up = BUTTONS[button]

        w = self._resolve(target)
        if w is None:
            return ActionResult.fail("click needs a window target")
        self._require_ax_input()
        wx, wy, ww, wh = self._bounds(w)
        gx, gy = wx + int(x), wy + int(y)          # window-relative -> global points
        self.activate(target)                       # a click on a bg app should focus it
        time.sleep(0.06)
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        pt = Quartz.CGPointMake(gx, gy)
        # WARP the real OS cursor to the point so hover/position is correct (Unity
        # reads the actual cursor). Then post down/up exactly like a known-good
        # auto-clicker (othyn/macos-auto-clicker): HID source, HID tap, no click-state
        # field, no pre-move — just the button pair at the cursor position. This drives
        # Unity UI buttons (Qud's toolbar, main menu) fine.
        # `hover=True`: some UIs — Qud's LEGACY console popups (the in-game menu, "press
        # [Space]" prompts) — activate the item under Input.mousePosition, which a warp
        # alone does NOT update. Post a real mouseMoved (approach + settle) first so the
        # target is hovered before the click. OFF by default: a pre-move BREAKS
        # world-cell clicks (Qud then hovers-but-never-selects the tile).
        if hover:
            approach = Quartz.CGPointMake(gx, gy - 24.0)
            Quartz.CGWarpMouseCursorPosition(approach)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved, approach, b))
            time.sleep(0.08)
            Quartz.CGWarpMouseCursorPosition(pt)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved, pt, b))
            time.sleep(0.2)
        else:
            Quartz.CGWarpMouseCursorPosition(pt)
            time.sleep(0.03)
        # Modifier-clicks (Cmd+Right-click = Raves' element-feedback gesture): set the flag on the
        # MOUSE events themselves. Flags-only is enough for Godot -- it reads event.meta_pressed off
        # the click -- and avoids the stuck-modifier class of bug entirely (nothing is ever held).
        flags = self._mod_flags(modifiers)
        # a stuck HID modifier (see _clear_stuck_mods) rides on mouse events too -- a plain
        # left click would arrive as Cmd+click. Clear anything we didn't ask for.
        self._clear_stuck_mods(keep=0)
        held = self._hold_mods(src, modifiers)
        try:
            for _ in range(2 if double else 1):
                ed = Quartz.CGEventCreateMouseEvent(src, down, pt, b)
                eu = Quartz.CGEventCreateMouseEvent(src, up, pt, b)
                if flags:
                    Quartz.CGEventSetFlags(ed, flags)
                    Quartz.CGEventSetFlags(eu, flags)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ed)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, eu)
                time.sleep(0.02)
        finally:
            self._release_mods(src, held)
        return ActionResult(ok=True, tier=4,
                            detail="%s%s%s click @ global (%d,%d)"
                                   % ("hover+" if hover else "", "double " if double else "",
                                      button, gx, gy))

    def scroll(self, target: str, x: int, y: int, dy: int = 1, dx: int = 0,
               modifiers: str = "") -> ActionResult:
        """Post a scroll-wheel event at a window point, optionally modifier-flagged.

        Written for a gesture no other op could reach: Raves' state-graph panel opens on
        **Ctrl+wheel**, and there was no way to drive a wheel at all, let alone a modified one.
        Working around that with a keyboard shortcut would have tested a different code path
        than the one the viewer uses.

        Same shape as `click`: warp the real cursor first (the wheel goes to whatever is under
        it, and Unity/Godot read the actual position), HID source and HID tap, and the modifier
        as a FLAG on the event rather than a held key — nothing to get stuck.

        `dy` is in LINES, positive = wheel-up/away. macOS reports line units as the coarse,
        universally-understood unit; pixel units would need a device profile to mean anything.
        """
        w = self._resolve(target)
        if w is None:
            return ActionResult.fail("scroll needs a window target")
        self._require_ax_input()
        wx, wy, ww, wh = self._bounds(w)
        gx, gy = wx + int(x), wy + int(y)
        self.activate(target)
        time.sleep(0.06)
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        pt = Quartz.CGPointMake(gx, gy)
        Quartz.CGWarpMouseCursorPosition(pt)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
            Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved, pt,
                                           Quartz.kCGMouseButtonLeft))
        time.sleep(0.05)
        flags = self._mod_flags(modifiers)
        self._clear_stuck_mods(keep=0)
        # MEASURED: flags alone are NOT enough for a modified WHEEL. Setting
        # kCGEventFlagMaskControl on the scroll event and posting it gets the wheel through to
        # Godot with ctrl_pressed FALSE — Raves' Ctrl+wheel panel never opened, while a plain
        # wheel zoomed the camera every time. (The flags-only trick documented on `click` is a
        # different path and stays as it is; do not "unify" them without re-measuring.)
        #
        # So for scroll we hold the REAL modifier around the event, which produces genuine
        # hardware modifier state. That is the stuck-modifier bug class the repo lost a day to,
        # so the release is in a `finally` and a belt-and-braces clear follows it: a modifier
        # orphaned DOWN makes every later synthetic key arrive modified and silently no-op,
        # surviving app restarts, and it is close to undiagnosable from the app side.
        held = self._hold_mods(src, modifiers)
        try:
            ev = Quartz.CGEventCreateScrollWheelEvent(src, Quartz.kCGScrollEventUnitLine,
                                                      2, int(dy), int(dx))
            if flags:
                Quartz.CGEventSetFlags(ev, flags)
            # The wheel event carries no position of its own — it lands wherever the cursor is,
            # which is why the warp above is not optional.
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)
        finally:
            self._release_mods(src, held)
        return ActionResult(ok=True, tier=4,
                            detail="scroll dy=%d dx=%d%s @ global (%d,%d)"
                                   % (int(dy), int(dx),
                                      (" mods=%s" % modifiers) if modifiers else "", gx, gy))

    # Virtual keycodes for the LEFT-hand modifier keys, for the cases where a flag on the
    # event is not enough and the key has to actually be held.
    _MOD_KEYCODE = {"cmd": 0x37, "meta": 0x37, "command": 0x37,
                    "ctrl": 0x3B, "control": 0x3B,
                    "shift": 0x38,
                    "alt": 0x3A, "opt": 0x3A, "option": 0x3A}

    @classmethod
    def _mod_keycodes(cls, mods: str) -> list:
        out = []
        for m in _split_mods(mods):
            kc = cls._MOD_KEYCODE.get(m.strip().lower())
            if kc is not None and kc not in out:
                out.append(kc)
        return out


    def mouse_move(self, target: str, x: int, y: int) -> ActionResult:
        """Warp + post a real mouseMoved at a window point — NO buttons. Activates
        the target first (Unity apps freeze/skip hover rendering when unfocused)."""
        w = self._resolve(target)
        if w is None:
            return ActionResult.fail("mouse needs a window target")
        self._require_ax_input()
        wx, wy, ww, wh = self._bounds(w)
        gx, gy = wx + int(x), wy + int(y)
        self.activate(target)
        time.sleep(0.06)
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        pt = Quartz.CGPointMake(gx, gy)
        Quartz.CGWarpMouseCursorPosition(pt)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
            Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved, pt,
                                           Quartz.kCGMouseButtonLeft))
        return ActionResult(ok=True, tier=4, detail="mouse @ global (%d,%d)" % (gx, gy))

    def inspect(self, target: str, depth: int = 3) -> Element:
        w = self._resolve(target)
        if w is None:
            raise BackendError("inspect needs a window target")
        ax, _pid = self._ax_window(w)
        return self._to_element(ax, depth)

    def ocr(self, target: str) -> dict:
        """Vision text recognition over the window capture. On-device, no network.
        The reader for AX-opaque apps (e.g. the ChatGPT desktop app)."""
        try:
            import Vision
            from Foundation import NSData
        except Exception as e:
            raise BackendError("OCR needs pyobjc Vision (pip install pyobjc-framework-Vision): %s" % e)
        png = self.screenshot(target)
        nsdata = NSData.dataWithBytes_length_(png, len(png))
        src = Quartz.CGImageSourceCreateWithData(nsdata, None)
        cg = None if src is None else Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if cg is None:
            raise BackendError("OCR: could not decode the capture")
        w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)          # 1 = accurate
        req.setUsesLanguageCorrection_(True)
        handler.performRequests_error_([req], None)
        boxes = []
        for r in (req.results() or []):
            cand = r.topCandidates_(1)
            if not cand:
                continue
            s = cand[0].string()
            bb = r.boundingBox()             # normalized, bottom-left origin
            x = int(bb.origin.x * w)
            y = int((1.0 - bb.origin.y - bb.size.height) * h)   # -> top-left px
            boxes.append({"text": str(s),
                          "bbox": [x, y, int(bb.size.width * w), int(bb.size.height * h)]})
        return {"w": int(w), "h": int(h), "boxes": boxes}

    def _to_element(self, el, depth) -> Element:
        if el is None:
            return Element(role="Unknown")
        role = self._ax_get(el, _ROLE) or "AXUnknown"
        name = self._ax_get(el, _TITLE) or self._ax_get(el, _DESC) or ""
        val = self._ax_get(el, _VALUE)
        val = "" if val is None else str(val)
        x, y = self._unwrap_axvalue(self._ax_get(el, _POS), point=True)
        ww, hh = self._unwrap_axvalue(self._ax_get(el, _SIZE), point=False)
        err, acts = AXUIElementCopyActionNames(el, None)
        el_out = Element(role=str(role), name=str(name), value=val,
                         x=x, y=y, w=ww, h=hh,
                         actions=[str(a) for a in (acts or [])] if err == 0 else [])
        if depth > 0:
            for k in (self._ax_get(el, _CHILDREN) or []):
                el_out.children.append(self._to_element(k, depth - 1))
        return el_out

    # ------------------------------------------------------------------- act
    def _running(self, pid: int):
        return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)

    def activate(self, target: str) -> ActionResult:
        w = self._resolve(target)
        if w is None:
            return ActionResult.fail("activate needs a window target")
        app = self._running(int(w["kCGWindowOwnerPID"]))
        if app is None:
            return ActionResult.fail("no running application for target")
        # COOPERATIVE ACTIVATION (macOS 14+, and by 26 it is the only thing that works).
        # `activateWithOptions_` returns YES and does NOTHING when the caller is not itself
        # frontmost -- the system now requires the activation to be handed over BY a running
        # app rather than seized. Measured 2026-08-08 on Darwin 25.5: three `hv activate
        # CavesOfQud` in a row each reported ok while the frontmost app stayed put, so every
        # click-driven edge posted its click into an unfocused window. Unity still delivers
        # mouseMoved (the tooltip appeared, which is what made this look like a coordinate
        # problem for half an hour) but ignores the button, so the edges failed with the
        # pointer provably on target.
        me = NSRunningApplication.currentApplication()
        moved = False
        if hasattr(app, "activateFromApplication_options_"):
            # 1|2 == ActivateAllWindows | ActivateIgnoringOtherApps
            moved = bool(app.activateFromApplication_options_(me, 1 | 2))
        if not moved:
            moved = bool(app.activateWithOptions_(1 | 2))   # pre-14 fallback
        # AND THEN CHECK. The API's return value is not evidence -- that is the whole bug
        # above. Poll who is actually frontmost and report THAT, so a failed activation
        # fails the step instead of handing the next click an unfocused window.
        want = int(w["kCGWindowOwnerPID"])
        deadline = time.time() + 1.5
        while time.time() < deadline:
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None and int(front.processIdentifier()) == want:
                return ActionResult(ok=True, tier=4,
                                    detail="activated (frontmost pid=%d)" % want)
            time.sleep(0.05)
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return ActionResult.fail(
            "activate did not take: asked for pid %d, frontmost is %s. macOS will refuse "
            "cross-app activation in some states; nothing that needs focus (clicks, keys) "
            "will work until it lands."
            % (want, (front.localizedName() if front else "?")))

    def move(self, target: str, x: int, y: int, w: int, h: int,
             topmost: Optional[bool] = None) -> ActionResult:
        win = self._resolve(target)
        if win is None:
            return ActionResult.fail("move needs a window target")
        ax, _pid = self._ax_window(win)
        if ax is None:
            return ActionResult.fail("no AX window to move")
        pt = AXValueCreate(kAXValueCGPointType, Quartz.CGPoint(x, y))
        sz = AXValueCreate(kAXValueCGSizeType, Quartz.CGSize(w, h))
        # Apply position→size twice. A cross-display move-and-grow otherwise clamps
        # the SIZE to the SOURCE display: the AXPosition change is async, so the
        # AXSize that follows lands while macOS still homes the window on the old
        # (smaller) monitor and shrinks it to fit. The first pass re-homes the
        # window on the target display; the second pass sets the real size there.
        # A final AXPosition re-asserts the corner, since a resize can nudge it.
        e1 = AXUIElementSetAttributeValue(ax, _POS, pt)
        e2 = AXUIElementSetAttributeValue(ax, _SIZE, sz)
        AXUIElementSetAttributeValue(ax, _POS, pt)
        e2 = AXUIElementSetAttributeValue(ax, _SIZE, sz)
        e1 = AXUIElementSetAttributeValue(ax, _POS, pt)
        # topmost: AX has no persistent always-on-top for another app's window.
        # True -> raise it now (best-effort, NOT sticky); False/None -> leave z-order.
        if topmost is True:
            AXUIElementPerformAction(ax, _RAISE)
        # VERIFY BY READBACK, not by the raw AX error codes: Godot's borderless window
        # returns kAXErrorFailure (-25200) from an AXSize set that actually LANDS (and
        # sometimes the reverse), so the codes alone both false-fail and false-pass.
        # The window frame in the CG window list is the ground truth — give the async
        # AX pipeline a beat, then compare requested vs actual (small tolerance).
        ok = (e1 == 0 and e2 == 0)
        actual = None
        for _ in range(6):                      # ~0.9s worst case
            time.sleep(0.15)
            cur = self._resolve(target)         # raw CGWindow dict — frame lives in kCGWindowBounds
            if cur is not None:
                bd = cur.get("kCGWindowBounds") or {}
                ax_, ay_ = int(bd.get("X", 1 << 30)), int(bd.get("Y", 1 << 30))
                aw_, ah_ = int(bd.get("Width", 0)), int(bd.get("Height", 0))
                actual = (ax_, ay_, aw_, ah_)
                if (abs(ax_ - x) <= 2 and abs(ay_ - y) <= 2
                        and abs(aw_ - w) <= 2 and abs(ah_ - h) <= 2):
                    ok = True
                    break
                ok = False
        note = "" if topmost is not True else " (topmost=raise-once; AX can't pin sticky)"
        detail = "AXPosition/AXSize %d,%d %dx%d%s" % (x, y, w, h, note)
        if ok:
            return ActionResult(ok=True, tier=1, detail=detail)
        return ActionResult(ok=False, tier=1, detail=detail,
                            error="move did not land (ax pos=%s size=%s, actual=%s)"
                                  % (e1, e2, actual))

    def text(self, target: str, text: str) -> ActionResult:
        w = self._resolve(target)
        if w is None:
            return ActionResult.fail("text needs a window target")
        ax, pid = self._ax_window(w)
        edit = self._find_editable(ax) or self._ax_get(ax, _FOCUSED_ELEM)
        # Tier 1: AX set the value on the editable element (focus-free), verify by readback.
        if edit is not None:
            err = AXUIElementSetAttributeValue(edit, _VALUE, text)
            if err == 0:
                time.sleep(0.03)
                got = self._ax_get(edit, _VALUE)
                if got is not None and text in str(got):
                    return ActionResult(ok=True, tier=1, detail="AXSetValue")
            tier1_err = "AXSetValue err=%s / readback mismatch" % err
        else:
            tier1_err = "no editable AX element found"
        # Tier 4: activate + type the characters via CGEvent (steals focus).
        self.activate(target)
        time.sleep(0.06)
        # GLOBAL tap, not per-pid: Godot ignores CGEventPostToPid exactly like Unity does (the
        # same reason the key op posts to the HID tap). Per-pid typing looked delivered -- ok:true
        # -- while the focused TextEdit never saw a character.
        for ch in text:
            self._post_char(ch, None)
        return ActionResult(ok=True, tier=4,
                            detail="activate + CGEvent typing, HID tap (tier1: %s)" % tier1_err)

    def key(self, target: str, keys: str, focus: bool = False) -> ActionResult:
        w = self._resolve(target)
        if w is None:
            return ActionResult.fail("key needs a window target")
        self._require_ax_input()
        pid = int(w["kCGWindowOwnerPID"])
        name = keys.strip()
        parts = [p for p in name.replace("-", "+").split("+") if p]
        mods, base = parts[:-1], (parts[-1] if parts else "")
        flags = self._mod_flags(mods)
        code = self._keycode_for(base)
        if code is None:
            return ActionResult.fail("unknown key %r" % keys)
        # Tier 4: activate + post to the HID event tap (global). Needed for apps that
        # read from the focused HID stream and ignore per-pid posts — Unity games
        # (Caves of Qud) and other engines. Steals focus, so it's opt-in.
        if focus:
            self.activate(target)
            time.sleep(0.06)
            self._post_key(code, flags, pid=None)
            return ActionResult(ok=True, tier=4,
                                detail="activate + CGEvent(HID) keycode=0x%02X flags=0x%X" % (code, flags))
        # Tier 2: post the key to the target pid without stealing focus. Effectiveness
        # varies by app; if it no-ops the caller can retry with focus=true.
        self._post_key(code, flags, pid=pid)
        return ActionResult(ok=True, tier=2,
                            detail="CGEventPostToPid keycode=0x%02X flags=0x%X" % (code, flags))

    # ---- CGEvent helpers ----
    def _mod_flags(self, mods) -> int:
        """Modifier NAMES (a list) or a spec string ("cmd,ctrl" / "ctrl+alt") -> CGEvent flags.

        Takes both because it has two kinds of caller: `key()` parses "ctrl+m" itself and hands
        over a list, while click/scroll receive the raw `modifiers` string off the wire. A second,
        string-only copy of this used to exist above; being defined FIRST it was shadowed by this
        one, so click/scroll were quietly iterating a string character by character.
        """
        m = 0
        for name in (_split_mods(mods) if isinstance(mods, str) else mods):
            u = name.upper()
            if u in ("CMD", "COMMAND", "META"): m |= Quartz.kCGEventFlagMaskCommand
            elif u in ("SHIFT",): m |= Quartz.kCGEventFlagMaskShift
            elif u in ("ALT", "OPTION", "OPT"): m |= Quartz.kCGEventFlagMaskAlternate
            elif u in ("CTRL", "CONTROL"): m |= Quartz.kCGEventFlagMaskControl
        return m

    def _keycode_for(self, base: str):
        u = base.upper()
        if u in KEYCODE:
            return KEYCODE[u]
        if len(base) == 1:
            return self._char_keycode(base)
        return None

    def _char_keycode(self, ch: str):
        return _US_KEYCODES.get(ch.lower())

    # Physical keycodes for the modifiers, so a combo can PRESS and RELEASE them for real.
    _MOD_KEYS = (
        (Quartz.kCGEventFlagMaskControl, 0x3B),
        (Quartz.kCGEventFlagMaskShift, 0x38),
        (Quartz.kCGEventFlagMaskAlternate, 0x3A),
        (Quartz.kCGEventFlagMaskCommand, 0x37),
    )

    def _clear_stuck_mods(self, keep: int = 0):
        """Release any modifier macOS believes is HELD that this op is not deliberately
        holding. The bracketing in _post_key leaves the modifier state clean -- unless the
        daemon dies BETWEEN the down and the up (the source watcher re-execs on any .py
        save, so an edit landing mid-combo orphans the down). The stuck flag then lives in
        the OS HID state: it survives app restarts, rides on EVERY later synthetic key
        ("e" arrives as Cmd+E and silently does nothing), and only a real keypress of that
        modifier clears it. Cost of finding this: a full day of 'the status screens
        intermittently refuse to open' across two Raves builds that were never at fault.
        Releasing is safe: if the flag is set because the HUMAN is holding the key mid-
        gesture, one synthetic up ends that gesture a beat early -- transient, and far
        cheaper than every scripted key silently no-opping."""
        state = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        for mask, kc in self._MOD_KEYS:
            if state & mask and not (keep & mask):
                ev = Quartz.CGEventCreateKeyboardEvent(src, kc, False)
                Quartz.CGEventSetFlags(ev, 0)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.02)

    def _post_key(self, keycode: int, flags: int, pid: Optional[int] = None):
        # HID-system source, same as click(): Unity's input system IGNORES keyboard
        # events synthesized with a None source (Qud's modern menus dropped every
        # injected Escape until this matched the known-good click path). Small gap
        # between down and up so per-frame pollers can't miss the pair.
        #
        # MODIFIERS ARE PRESSED AND RELEASED FOR REAL, not smuggled onto the key event.
        # Setting kCGEventFlagMaskControl on a keyboard event posted to the HID tap makes
        # macOS believe Control is HELD, and nothing here ever cleared it — so after one
        # `hv key <win> ctrl+tab` EVERY subsequent key arrived modified. A later plain "n"
        # reached the app as Ctrl+N and silently did nothing, which is exactly how Raves'
        # status-screen openers "intermittently" stopped working: they stopped the moment a
        # combo was first sent and stayed broken for the life of the session. Bracketing the
        # key with genuine modifier down/up leaves the global modifier state as we found it.
        self._clear_stuck_mods(keep=flags)
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)

        def _post(ev):
            if pid:
                Quartz.CGEventPostToPid(pid, ev)
            else:
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.02)

        mods = [(mask, kc) for mask, kc in self._MOD_KEYS if flags & mask]
        acc = 0
        for mask, kc in mods:                      # press each modifier
            acc |= mask
            ev = Quartz.CGEventCreateKeyboardEvent(src, kc, True)
            Quartz.CGEventSetFlags(ev, acc)
            _post(ev)
        for down in (True, False):                 # the key itself
            ev = Quartz.CGEventCreateKeyboardEvent(src, keycode, down)
            if flags:
                Quartz.CGEventSetFlags(ev, flags)
            _post(ev)
        for mask, kc in reversed(mods):            # release, innermost first
            acc &= ~mask
            ev = Quartz.CGEventCreateKeyboardEvent(src, kc, False)
            Quartz.CGEventSetFlags(ev, acc)
            _post(ev)

    def _post_char(self, ch: str, pid: Optional[int] = None):
        """Type one character by its unicode (keycode 0 + a unicode payload) so any
        printable char works regardless of keyboard layout."""
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            if pid:
                Quartz.CGEventPostToPid(pid, ev)
            else:
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


# US ANSI keyboard: char -> virtual keycode, for named keys / combos. Plain text
# uses unicode typing (_post_char) instead, so this only needs the common set.
_US_KEYCODES = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
    "6": 0x16, "5": 0x17, "9": 0x19, "7": 0x1A, "8": 0x1C, "0": 0x1D, "o": 0x1F,
    "u": 0x20, "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D,
    "m": 0x2E, "=": 0x18, "-": 0x1B, "]": 0x1E, "[": 0x21, "'": 0x27, ";": 0x29,
    "\\": 0x2A, ",": 0x2B, "/": 0x2C, ".": 0x2F, "`": 0x32,
}
