"""WindowsBackend — observe + control via UI Automation (UIA) and Win32.

Built on what the Slice 0 spike proved (see spike/README.md):
  - UIA ValuePattern.SetValue writes to an UNFOCUSED window (tier 1)
  - SendMessage(WM_SETTEXT) to a child EDIT hwnd (tier 2)
  - DPI-aware PrintWindow(PW_RENDERFULLCONTENT) captures a specific window
  - 64-bit ctypes needs explicit argtypes/restype or handles truncate

UIA (via the ``uiautomation`` package) does enumeration, inspection, and the
semantic tier-1 actions. Raw Win32 (ctypes) does capture, message posting, and
activation. All calls happen on the engine's single worker thread, which owns the
COM apartment — so no cross-thread COM headaches.
"""
import ctypes
import time
from ctypes import wintypes
from typing import List, Optional

import uiautomation as auto

from ..backend import ActionResult, BackendError, Element, PlatformBackend, Target

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# --- Win32 constants -------------------------------------------------------
WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x000C, 0x000D, 0x000E
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
PW_RENDERFULLCONTENT = 0x00000002
SW_RESTORE = 9

# BitBlt raster op + SetWindowPos flags / special HWNDs (window-ops).
SRCCOPY = 0x00CC0020
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
HWND_TOP, HWND_TOPMOST, HWND_NOTOPMOST = 0, -1, -2
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001

# mouse_event down/up pairs per button. Middle matters more than it looks: Qud's Map Editor
# dispatches a "MiddleTile:x,y" command on it, and an app can hang real behaviour off a button
# no amount of left/right clicking will reach.
_BUTTON_EVENTS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}

VK = {
    "RETURN": 0x0D, "ENTER": 0x0D, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B,
    "SPACE": 0x20, "BACKSPACE": 0x08, "BACK": 0x08, "DELETE": 0x2E, "DEL": 0x2E,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "INSERT": 0x2D,
}
for _i in range(1, 13):
    VK["F%d" % _i] = 0x70 + (_i - 1)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _configure_win32():
    """Set restype/argtypes for every handle-bearing call (64-bit correctness)."""
    sig = [
        (user32.GetWindowDC, ctypes.c_void_p, [wintypes.HWND]),
        (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, ctypes.c_void_p]),
        (user32.PrintWindow, ctypes.c_int,
         [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]),
        (user32.GetForegroundWindow, wintypes.HWND, []),
        (user32.SetForegroundWindow, ctypes.c_int, [wintypes.HWND]),
        (user32.GetWindowRect, ctypes.c_int, [wintypes.HWND, ctypes.c_void_p]),
        (user32.IsIconic, ctypes.c_int, [wintypes.HWND]),
        (user32.ShowWindow, ctypes.c_int, [wintypes.HWND, ctypes.c_int]),
        (user32.SendMessageW, ctypes.c_long,
         [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (user32.PostMessageW, ctypes.c_int,
         [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (gdi32.CreateCompatibleDC, ctypes.c_void_p, [ctypes.c_void_p]),
        (gdi32.CreateCompatibleBitmap, ctypes.c_void_p,
         [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
        (gdi32.SelectObject, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
        (gdi32.DeleteObject, ctypes.c_int, [ctypes.c_void_p]),
        (gdi32.DeleteDC, ctypes.c_int, [ctypes.c_void_p]),
        (gdi32.GetDIBits, ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
          ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]),
        # window-ops: screen capture (BitBlt), screen size, move, robust activate
        (user32.GetDC, ctypes.c_void_p, [wintypes.HWND]),
        (user32.GetSystemMetrics, ctypes.c_int, [ctypes.c_int]),
        (user32.SetWindowPos, ctypes.c_int,
         [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ctypes.c_int, wintypes.UINT]),
        (user32.GetWindowThreadProcessId, wintypes.DWORD,
         [wintypes.HWND, ctypes.c_void_p]),
        (user32.AttachThreadInput, ctypes.c_int,
         [wintypes.DWORD, wintypes.DWORD, ctypes.c_int]),
        (user32.BringWindowToTop, ctypes.c_int, [wintypes.HWND]),
        (user32.SystemParametersInfoW, ctypes.c_int,
         [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]),
        (gdi32.BitBlt, ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
          wintypes.DWORD]),
        # SHORT, not int: the high byte carries the shift state and the low byte the VK,
        # and -1 means "this character has no key on the current layout".
        (user32.VkKeyScanW, ctypes.c_short, [ctypes.c_wchar]),
    ]
    for fn, res, args in sig:
        fn.restype, fn.argtypes = res, args



DPI_STATUS = "unset"


def _dpi_aware():
    """Force physical-pixel coordinates. ctypes does NOT raise on a FALSE return,
    so the old try/except chain could 'succeed' while leaving the process DPI-
    virtualized — one daemon generation then reports doubled window rects and
    captures the wrong desktop area (observed: a 3232x1878 window listed as
    6954x3912 after a self-restart). Check returns, walk every fallback, and
    surface the outcome in ping as dpi_status."""
    global DPI_STATUS
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_V2
            DPI_STATUS = "per-monitor-v2"
            return
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # S_OK
            DPI_STATUS = "per-monitor"
            return
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            DPI_STATUS = "system"
            return
    except Exception:
        pass
    # Setting can legitimately fail when awareness was already fixed (manifest,
    # or a prior call this process) — report what we actually run under.
    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        DPI_STATUS = "preset(ctx=%s)" % (ctx if isinstance(ctx, int) else "?")
    except Exception:
        DPI_STATUS = "unknown"


class WindowsBackend(PlatformBackend):
    name = "windows"

    def thread_init(self):
        # Must run before creating windows/UIA on this thread.
        _dpi_aware()
        _configure_win32()
        # Initialize COM on THIS worker thread before any UIA call. comtypes' lazy
        # init doesn't always fire when the daemon is spawned via subprocess (vs a
        # shell), surfacing as "CoInitialize has not been called" from
        # UIAutomationCore. Do it explicitly so the daemon starts either way.
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        # Touch UIA once so comtypes finishes initializing on THIS (worker) thread.
        auto.GetRootControl()

    # ---------------------------------------------------------------- helpers
    def _toplevels(self):
        """Top-level window controls that are real, on-screen, and named."""
        out = []
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ControlTypeName not in ("WindowControl", "PaneControl"):
                    continue
                if not w.NativeWindowHandle:
                    continue
                out.append(w)
            except Exception:
                continue
        return out

    def _resolve(self, ref: Optional[str]):
        """Turn a target ref into a top-level HWND (int).
        Accepts: None/"screen" -> None; "hwnd:0x1a2b"/"hwnd:123"; "pid:123";
        else a case-insensitive title substring."""
        if ref is None or ref == "screen":
            return None
        if ref.startswith("hwnd:"):
            v = ref.split(":", 1)[1]
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        if ref.startswith("pid:"):
            pid = int(ref.split(":", 1)[1])
            for w in self._toplevels():
                if w.ProcessId == pid:
                    return w.NativeWindowHandle
            raise BackendError("no window for pid %d" % pid)
        low = ref.lower()
        for w in self._toplevels():
            # Match the REAL Win32 caption first (what `ls` shows since the title
            # fix — Godot's UIA Name says "Godot Engine" while the caption says
            # "Raves of Qud (DEBUG)"), with the UIA Name as fallback.
            title = self._win32_title(w.NativeWindowHandle) or w.Name or ""
            if low in title.lower():
                return w.NativeWindowHandle
        raise BackendError("no window matching title ~ %r" % ref)

    def _find_editable(self, ctrl, depth=8):
        """DFS for an edit/document descendant (for text/key delivery)."""
        try:
            if ctrl.ControlTypeName in ("EditControl", "DocumentControl"):
                return ctrl
        except Exception:
            pass
        if depth <= 0:
            return None
        try:
            kids = ctrl.GetChildren()
        except Exception:
            return None
        for k in kids:
            hit = self._find_editable(k, depth - 1)
            if hit is not None:
                return hit
        return None

    # ----------------------------------------------------------------- observe
    def displays(self):
        """Every active monitor in the physical-pixel virtual-desktop space that
        ``move``/window rects use (we're DPI-aware), via EnumDisplayMonitors —
        a secondary monitor reports its real origin (e.g. x=3840 to the right of
        a 4K primary). ``main`` marks the primary (MONITORINFOF_PRIMARY)."""
        out = []

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def _cb(hmon, _hdc, _lprc, _lparam):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                out.append({"id": len(out), "x": r.left, "y": r.top,
                            "w": r.right - r.left, "h": r.bottom - r.top,
                            "main": bool(mi.dwFlags & 1)})  # MONITORINFOF_PRIMARY
            return 1

        user32.EnumDisplayMonitors(None, None, MonitorEnumProc(_cb), 0)
        return out

    def _win32_title(self, hwnd) -> str:
        """The real Win32 caption. UIA's Name can differ from it — Godot names its
        accessibility root "Godot Engine" while the caption says "Raves of Qud
        (DEBUG)" — and substring targeting must match what the titlebar shows."""
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def list_targets(self) -> List[Target]:
        """Enumerate top-level windows through WIN32, never UIA.

        UIA reads (BoundingRectangle / Name / ClassName / ProcessId / IsOffscreen) are
        cross-process COM calls into the target's UI thread. When that thread is not
        pumping — Qud during world generation, or sitting on a modal — they BLOCK, and a
        blocking COM call does not raise, so the per-item try/except around them bought
        nothing: one stalled app wedged the whole daemon and every later op timed out.
        Observed three times in one session on Lumpy; each needed a daemon restart.

        The Win32 calls below read from the window manager rather than from the app, so a
        hung process yields a stale rect instead of hanging us. `hv move` already verifies
        against GetWindowRect, so listing now agrees with what move reports.

        UIA is still the right tool for `inspect` (it is the only thing that knows about
        elements) — it just has no business in the listing path.
        """
        fg = user32.GetForegroundWindow()
        out = []
        buf = ctypes.create_unicode_buffer(256)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _each(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                w_, h_ = rect.right - rect.left, rect.bottom - rect.top
                title = self._win32_title(hwnd) or ""
                # UIA's top-level view used to hide the OS's swarm of 1x1 untitled helper
                # windows for us; EnumWindows does not, so drop them here. A real untitled
                # window (some Godot/Unity surfaces) is still kept on size alone.
                if not title and (w_ < 50 or h_ < 50):
                    return True
                user32.GetClassNameW(hwnd, buf, 256)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append(Target(
                    id="hwnd:0x%X" % hwnd, kind="window", pid=int(pid.value),
                    title=title, class_name=buf.value or "",
                    x=rect.left, y=rect.top, w=w_, h=h_,
                    focused=(hwnd == fg), visible=True))
            except Exception:
                pass
            return True

        user32.EnumWindows(_each, 0)
        return out

    def launch(self, spec: str, args=None) -> ActionResult:
        import os
        import subprocess
        spec = (spec or "").strip()
        if not spec:
            return ActionResult.fail("empty launch spec")
        args = [str(a) for a in (args or [])]
        try:
            if args:
                # os.startfile can't pass argv; Popen the target with its args.
                subprocess.Popen([spec] + args)
            else:
                os.startfile(spec)  # a path, a URL scheme (steam://…), or a doc/app
        except Exception as e:
            return ActionResult.fail("launch failed: %s" % e)
        return ActionResult(ok=True, detail="launch %s" % " ".join([spec] + args))

    def mouse_move(self, target: str, x: int, y: int) -> ActionResult:
        """Pure hover: warp + a REAL injected move, no buttons.

        The darwin backend has had this since OP_MOUSE was defined; Windows never
        implemented it, so `hv mouse` died with an AttributeError and every step that
        wanted a hover had to fake one with a click. That is not a cosmetic gap --
        Qud's chargen carousel SELECTS the card under the cursor and CONFIRMS on a
        click anywhere else, so hovering and clicking are two different verbs there
        and a driver that only has the second cannot choose a card deterministically.

        Same MOUSEEVENTF_ABSOLUTE move as click() and for the same reason: SetCursorPos
        alone raises no WM_INPUT, and Unity's Input System syncs its pointer only from
        raw moves, so a warped cursor leaves Input.mousePosition stale. Deliberately
        does NOT activate the window -- a hover that steals focus would change the
        state it exists to observe.
        """
        hwnd = self._resolve(target)
        if hwnd is None:
            return ActionResult.fail("mouse needs a window target")
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        gx, gy = rect.left + int(x), rect.top + int(y)
        sw, sh = self.screen_size()
        user32.SetCursorPos(gx, gy)
        user32.mouse_event(0x0001 | 0x8000,
                           int(gx * 65535 / max(sw - 1, 1)),
                           int(gy * 65535 / max(sh - 1, 1)), 0, 0)
        time.sleep(0.12)
        return ActionResult(ok=True, tier=4, detail="moved @ (%d,%d)" % (gx, gy))

    def click(self, target: str, x: int, y: int, button: str = "left",
              double: bool = False, hover: bool = False,
              modifiers: Optional[str] = None) -> ActionResult:
        hwnd = self._resolve(target)
        if hwnd is None:
            return ActionResult.fail("click needs a window target")
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        gx, gy = rect.left + int(x), rect.top + int(y)   # window-relative -> screen px
        self.activate(target)
        time.sleep(0.06)
        # Move with a REAL injected event, not just SetCursorPos: SetCursorPos
        # produces no raw input (WM_INPUT), and Unity's Input System only syncs
        # its pointer from raw moves — a warp-then-click lands at Unity's STALE
        # position (observed as "first click ignored" on Qud's title, Win11
        # 2026-08-06). MOUSEEVENTF_ABSOLUTE coords are 0..65535 across the
        # primary display.
        MOVE_ABS = 0x0001 | 0x8000
        sw, sh = self.screen_size()

        def _move(px, py):
            user32.SetCursorPos(px, py)
            user32.mouse_event(MOVE_ABS, int(px * 65535 / max(sw - 1, 1)),
                               int(py * 65535 / max(sh - 1, 1)), 0, 0)

        # `hover=True` mirrors the darwin backend: Unity legacy-console UIs (Qud's
        # menus) select the item under Input.mousePosition, which needs the cursor
        # hovered and settled BEFORE the button pair — approach from above, then
        # rest on the target. OFF by default: a pre-move breaks world-cell clicks.
        if hover:
            _move(gx, gy - 24)
            time.sleep(0.08)
            _move(gx, gy)
            time.sleep(0.2)
        else:
            _move(gx, gy)
            time.sleep(0.02)
        # MODIFIERS held across the button pair (ctrl / alt / shift, "+"-joined).
        # Qud's Map Editor makes these core verbs — Ctrl+Click paints from the
        # palette, Alt+Click samples back into it — so a click without them can
        # only ever look at that screen, never drive it. Scan-code SendInput for
        # the same reason `key --focus` uses it: Unity's raw input drops VK-only
        # synthetics. Always released in a finally, so a failure mid-click cannot
        # leave a modifier stuck down for the whole desktop.
        mods = [m.strip().lower() for m in (modifiers or "").split("+") if m.strip()]
        vk_for = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10}
        held = [vk_for[m] for m in mods if m in vk_for]
        try:
            if held:
                # The modifier must be delivered to a window that ALREADY has focus, and it
                # must still be down when the app PROCESSES the click (message handling lags
                # injection), or a message-tracking toolkit reports no modifier at all.
                self._await_focus(hwnd)
                for vk in held:
                    self._send_modifier(vk, True)
                time.sleep(0.06)
            dn, up = _BUTTON_EVENTS.get(button, _BUTTON_EVENTS["left"])
            for _ in range(2 if double else 1):
                user32.mouse_event(dn, 0, 0, 0, 0)
                user32.mouse_event(up, 0, 0, 0, 0)
                time.sleep(0.02)
            if held:
                time.sleep(0.12)   # let the click be consumed before the modifier lifts
        finally:
            for vk in reversed(held):
                self._send_modifier(vk, False)
        return ActionResult(ok=True, tier=4,
                            detail="%s%s%s click @ (%d,%d)"
                                   % ("+".join(mods) + " " if mods else "",
                                      "double " if double else "", button, gx, gy))

    def drag(self, target: str, x1: int, y1: int, x2: int, y2: int,
             button: str = "left", steps: int = 12,
             modifiers: Optional[str] = None, hold: float = 0.08) -> ActionResult:
        """Press at (x1,y1), move in steps, release at (x2,y2) — window-relative.

        A drag is not a click pair: apps that track a selection rectangle need the
        INTERMEDIATE moves (Qud's Map Editor builds SelectedRegion from OnBeginDrag/
        OnDragMove, and only a dragged region populates its selected-contents list),
        and Unity only sees moves that carry raw input, hence mouse_event rather than
        SetCursorPos. Modifiers are held for the whole gesture and released in a
        finally, same contract as click()."""
        hwnd = self._resolve(target)
        if hwnd is None:
            return ActionResult.fail("drag needs a window target")
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        gx1, gy1 = rect.left + int(x1), rect.top + int(y1)
        gx2, gy2 = rect.left + int(x2), rect.top + int(y2)
        self.activate(target)
        time.sleep(0.06)
        MOVE_ABS = 0x0001 | 0x8000
        sw, sh = self.screen_size()

        def _move(px, py):
            user32.SetCursorPos(px, py)
            user32.mouse_event(MOVE_ABS, int(px * 65535 / max(sw - 1, 1)),
                               int(py * 65535 / max(sh - 1, 1)), 0, 0)

        mods = [m.strip().lower() for m in (modifiers or "").split("+") if m.strip()]
        vk_for = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10}
        held = [vk_for[m] for m in mods if m in vk_for]
        dn, up = _BUTTON_EVENTS.get(button, _BUTTON_EVENTS["left"])
        try:
            if held:
                self._await_focus(hwnd)   # same rule as click(): focus first, then modifiers
            for vk in held:
                self._send_vk_scancode(vk, down=True, up=False)
            _move(gx1, gy1)
            time.sleep(0.08)
            user32.mouse_event(dn, 0, 0, 0, 0)
            # HOLD before moving, in seconds. MEASURED (Qud Map Editor, 2026-08-06):
            # LONGER IS WORSE. At the 0.08 default a Ctrl+drag paints; at 0.5 or 1.2
            # the identical gesture paints NOTHING — a long press before movement
            # stops registering as a drag at all (it reads as press-and-hold). So do
            # not raise this hoping to make a stubborn drag land; it is exposed to be
            # LOWERED, or raised only for an app measured to want it.
            time.sleep(max(0.0, float(hold)))
            n = max(2, int(steps))
            for i in range(1, n + 1):
                _move(gx1 + (gx2 - gx1) * i // n, gy1 + (gy2 - gy1) * i // n)
                time.sleep(0.03)
            time.sleep(0.08)
            user32.mouse_event(up, 0, 0, 0, 0)
        finally:
            for vk in reversed(held):
                self._send_modifier(vk, False)
        return ActionResult(ok=True, tier=4,
                            detail="%sdrag %s (%d,%d)->(%d,%d)"
                                   % ("+".join(mods) + " " if mods else "",
                                      button, gx1, gy1, gx2, gy2))

    def screenshot(self, target: Optional[str], native: bool = False) -> bytes:
        # `native` is a macOS/ScreenCaptureKit distinction; the Windows path is
        # already physical-pixel (DPI-aware), so it's accepted and ignored.
        from io import BytesIO
        from PIL import Image
        hwnd = self._resolve(target)
        if hwnd is None:
            # Full-screen: PrintWindow on the desktop returns black, so BitBlt
            # from the screen DC instead. Physical pixels (we're DPI-aware).
            w, h = self.screen_size()
            src = user32.GetDC(None)
            mem = gdi32.CreateCompatibleDC(src)
            bmp = gdi32.CreateCompatibleBitmap(src, w, h)
            gdi32.SelectObject(mem, bmp)
            gdi32.BitBlt(mem, 0, 0, w, h, src, 0, 0, SRCCOPY)
            buf = self._dib_bytes(mem, bmp, w, h)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, src)
            out = BytesIO()
            Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).save(out, "PNG")
            return out.getvalue()
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            raise BackendError("window has no area (minimized?)")
        hdc = user32.GetWindowDC(hwnd)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mem, bmp)
        user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
        buf = self._dib_bytes(mem, bmp, w, h)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
        out = BytesIO()
        Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).save(out, "PNG")
        return out.getvalue()

    def _dib_bytes(self, mem, bmp, w, h):
        """Pull top-down 32-bit BGRX pixels out of a bitmap via GetDIBits."""
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        return buf

    def screen_size(self):
        return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))

    def move(self, target: str, x: int, y: int, w: int, h: int,
             topmost: Optional[bool] = None) -> ActionResult:
        hwnd = self._resolve(target)
        if hwnd is None:
            return ActionResult.fail("move needs a window target")
        # A minimized *or* maximized window ignores SetWindowPos geometry (the
        # zoom/iconic state wins), so clear it first — otherwise "tile to a half"
        # silently leaves a maximized window full-screen.
        if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # Tri-state z-order: True pins topmost, False explicitly clears the
        # topmost bit, None just raises (HWND_TOP leaves an existing topmost
        # window topmost — there's no way to clear it without HWND_NOTOPMOST).
        insert_after = (HWND_TOPMOST if topmost is True else
                        HWND_NOTOPMOST if topmost is False else HWND_TOP)
        flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
        ok = user32.SetWindowPos(hwnd, insert_after, x, y, w, h, flags)
        return ActionResult(ok=bool(ok), tier=4,
                            detail="SetWindowPos %d,%d %dx%d topmost=%s"
                                   % (x, y, w, h, topmost))

    def inspect(self, target: str, depth: int = 3) -> Element:
        hwnd = self._resolve(target)
        if hwnd is None:
            raise BackendError("inspect needs a window target")
        return self._to_element(auto.ControlFromHandle(hwnd), depth)

    def _to_element(self, ctrl, depth) -> Element:
        try:
            r = ctrl.BoundingRectangle
            el = Element(role=ctrl.ControlTypeName, name=ctrl.Name or "",
                         x=r.left, y=r.top, w=r.width(), h=r.height())
        except Exception:
            el = Element(role="Unknown")
        try:
            vp = ctrl.GetValuePattern()
            if vp is not None:
                el.value = vp.Value or ""
                el.actions.append("SetValue")
        except Exception:
            pass
        if depth > 0:
            try:
                for k in ctrl.GetChildren():
                    el.children.append(self._to_element(k, depth - 1))
            except Exception:
                pass
        return el

    # ------------------------------------------------------------------ act
    def activate(self, target: str) -> ActionResult:
        hwnd = self._resolve(target)
        if hwnd is None:
            return ActionResult.fail("activate needs a window target")
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # A bare SetForegroundWindow from a background process is refused by the
        # foreground lock (returns 0). Defeat it: zero the lock timeout, then
        # attach our input queue to the current foreground thread's so Windows
        # treats the call as coming from the active app.
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                     ctypes.c_void_p(0), 0)
        fg = user32.GetForegroundWindow()
        tgt_t = user32.GetWindowThreadProcessId(hwnd, None)
        fg_t = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(fg_t) and fg_t != tgt_t
        if attached:
            user32.AttachThreadInput(fg_t, tgt_t, True)
        try:
            user32.BringWindowToTop(hwnd)
            ok = user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_t, tgt_t, False)
        return ActionResult(ok=bool(ok), tier=4,
                            detail="SetForegroundWindow=%s (attach=%s)"
                                   % (ok, attached))

    def _wm_get_text(self, hwnd):
        n = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.SendMessageW(hwnd, WM_GETTEXT, n + 1, ctypes.addressof(b))
        return b.value

    def text(self, target: str, text: str) -> ActionResult:
        hwnd = self._resolve(target)
        if hwnd is None:
            return ActionResult.fail("text needs a window target")
        edit = self._find_editable(auto.ControlFromHandle(hwnd))
        if edit is None:
            return ActionResult.fail("no editable element found in target")
        edit_hwnd = edit.NativeWindowHandle

        # Tier 1: UIA ValuePattern (semantic, focus-free) — verify by readback.
        try:
            vp = edit.GetValuePattern()
            vp.SetValue(text)
            time.sleep(0.05)
            got = ""
            try:
                got = edit.GetValuePattern().Value or ""
            except Exception:
                got = self._wm_get_text(edit_hwnd) if edit_hwnd else ""
            if text in got:
                return ActionResult(ok=True, tier=1, detail="UIA ValuePattern.SetValue")
        except Exception as e:
            tier1_err = str(e)
        else:
            tier1_err = "readback mismatch"

        # Tier 2: WM_SETTEXT to the child edit hwnd.
        if edit_hwnd:
            try:
                user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(text))
                time.sleep(0.05)
                if text in (self._wm_get_text(edit_hwnd) or ""):
                    return ActionResult(ok=True, tier=2, detail="WM_SETTEXT")
            except Exception as e:
                return ActionResult(ok=False, tier=None,
                                    error="tier1(%s) tier2(%s)" % (tier1_err, e))
        return ActionResult(ok=False, tier=None,
                            error="tier1(%s); no child hwnd for tier2" % tier1_err)

    def _await_focus(self, hwnd, timeout: float = 1.0) -> bool:
        """Block until ``hwnd`` is actually the foreground window.

        activate() returns as soon as it has ASKED Windows to raise the window; focus
        lands a moment later. That gap matters for modifiers: a synthetic Ctrl goes to
        whatever is focused RIGHT NOW, and an app that tracks modifier state from its own
        WM_KEYDOWN messages (Godot) never sees a keystroke delivered before it had focus —
        so InputEventMouseButton.ctrl_pressed stays false while the click itself still
        lands. Unity hides this by polling global key state instead, which is why the same
        gesture worked against Qud and silently failed against Raves."""
        end = time.time() + timeout
        while time.time() < end:
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.02)
        return user32.GetForegroundWindow() == hwnd

    def _send_modifier(self, vk: int, down: bool) -> None:
        """Hold/release a modifier so BOTH toolkits see it.

        MEASURED (2026-08-07): a scancode-only injection (KEYEVENTF_SCANCODE, which is what
        Qud needs for Space/Escape) is invisible to Godot — Ctrl+click arrived with
        ctrl_pressed false and Ctrl+A did nothing, while uiautomation's VK-based SendKeys
        drove the same shortcut fine. Unity polls global key state and accepts either;
        Godot tracks modifiers from the key messages it receives and wants the VK form.
        Rather than pick a winner, emit BOTH: modifiers are level-triggered, so a doubled
        press/release is harmless, and each toolkit sees the form it recognises."""
        # uiautomation's PressKey/ReleaseKey is the delivery that DEMONSTRABLY reaches Godot
        # (its SendKeys drove Ctrl+A when a raw keybd_event/scancode press did not), so use it
        # for the VK half rather than rolling our own; the scancode half stays for Unity.
        try:
            if down:
                auto.PressKey(vk)
            else:
                auto.ReleaseKey(vk)
        except Exception:
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(vk, user32.MapVirtualKeyW(vk, 0),
                               0 if down else KEYEVENTF_KEYUP, 0)
        self._send_vk_scancode(vk, down=down, up=not down)               # scancode form (Unity)

    def _send_vk_scancode(self, vk: int, down: bool = True, up: bool = True) -> None:
        """Press and/or release one virtual key via SendInput WITH its hardware scan
        code. ``down``/``up`` split the pair so a modifier can be HELD across another
        event (Ctrl+Click); the default sends both, i.e. a tap.
        Games reading raw input (Unity's Input System) need the scan code; a
        VK-only event is silently dropped for some keys. Arrow/nav keys are
        extended-flag keys — without KEYEVENTF_EXTENDEDKEY they alias numpad."""
        KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
        extended = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if vk in extended else 0)
        down = INPUT(type=1)  # INPUT_KEYBOARD
        down.ki = KEYBDINPUT(vk, scan, flags, 0, None)
        up = INPUT(type=1)
        up.ki = KEYBDINPUT(vk, scan, flags | KEYEVENTF_KEYUP, 0, None)
        arr = (INPUT * 2)(down, up)
        user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        time.sleep(0.03)

    def key(self, target: str, keys: str, focus: bool = False) -> ActionResult:
        hwnd = self._resolve(target)
        if hwnd is None:
            return ActionResult.fail("key needs a window target")
        name = keys.strip()
        upper = name.upper()
        # Tier 4 (opt-in): activate + SendInput for apps that ignore PostMessage
        # (Unity/games). Named keys go out as REAL scan-code events — Unity's raw
        # Input System drops some VK-only synthetics (Space/Esc arrived dead from
        # SendKeys while Enter/arrows worked; scan codes fixed it, Qud 1.0.5,
        # Win11 2026-08-06). Unnamed multi-char sequences still fall back to
        # SendKeys.
        if focus:
            try:
                self.activate(target)
                time.sleep(0.06)
                if upper in VK:
                    self._send_vk_scancode(VK[upper])
                    return ActionResult(ok=True, tier=4,
                                        detail="activate + SendInput scancode VK 0x%02X" % VK[upper])
                auto.SendKeys(keys, waitTime=0)
                return ActionResult(ok=True, tier=4, detail="activate + SendKeys %s" % keys)
            except Exception as e:
                return ActionResult.fail("SendKeys failed: %s" % e)
        # Deliver to the editable child if present, else the top-level window.
        edit = self._find_editable(auto.ControlFromHandle(hwnd))
        dest = (edit.NativeWindowHandle if edit and edit.NativeWindowHandle
                else hwnd)
        name = keys.strip()
        upper = name.upper()

        # Tier 2: post a named virtual key, or a single printable char.
        if upper in VK:
            vk = VK[upper]
            user32.PostMessageW(dest, WM_KEYDOWN, vk, 0)
            user32.PostMessageW(dest, WM_KEYUP, vk, 0)
            return ActionResult(ok=True, tier=2, detail="PostMessage VK 0x%02X" % vk)
        if len(name) == 1:
            # A single printable character used to go out as WM_CHAR ALONE, always. That is a
            # TEXT message carrying no virtual-key, so anything dispatching on a KEY — Godot's
            # `event.keycode`, Unity's Input System — saw nothing, while text fields worked
            # perfectly. Silent and asymmetric: `hv key win r` typed an "r" into a LineEdit but
            # could not fire an `event.keycode == KEY_R` binding, and still reported ok.
            #
            # The two destinations want DIFFERENT messages, which is why this is not simply
            # "post the whole WM_KEYDOWN -> WM_CHAR -> WM_KEYUP sequence a real keystroke
            # produces". Measured on Raves (Godot) 2026-08-08: posting all three typed every
            # character TWICE ("base" -> "bbaassee"), because Godot translates WM_KEYDOWN into
            # text itself and then takes the WM_CHAR as a second character.
            #
            #   - a real EDIT child: inserts on WM_CHAR; a bare WM_KEYDOWN inserts nothing
            #   - a top-level window (Godot/Unity, which expose no editable child): translates
            #     the key itself, so the VK alone yields BOTH the keycode and the character
            if edit is not None and edit.NativeWindowHandle:
                user32.PostMessageW(dest, WM_CHAR, ord(name), 0)
                return ActionResult(ok=True, tier=2, detail="PostMessage WM_CHAR %r (edit)" % name)
            vks = user32.VkKeyScanW(name)
            if vks != -1:
                vk = vks & 0xFF
                user32.PostMessageW(dest, WM_KEYDOWN, vk, 0)
                user32.PostMessageW(dest, WM_KEYUP, vk, 0)
                return ActionResult(ok=True, tier=2, detail="PostMessage VK 0x%02X (%r)" % (vk, name))
            # No key produces this character on the current layout — text only is all there is.
            user32.PostMessageW(dest, WM_CHAR, ord(name), 0)
            return ActionResult(ok=True, tier=2, detail="PostMessage WM_CHAR %r (no VK)" % name)

        # Tier 4: combos / long sequences — activate then send globally.
        # (PostMessage can't carry modifier state reliably; see research findings.)
        try:
            self.activate(target)
            time.sleep(0.05)
            auto.SendKeys(keys, waitTime=0)
            return ActionResult(ok=True, tier=4, detail="activate + SendKeys")
        except Exception as e:
            return ActionResult.fail("no tier could deliver keys %r: %s" % (keys, e))
