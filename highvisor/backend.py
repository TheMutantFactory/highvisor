"""PlatformBackend — the interface every OS backend implements, plus the small
data model the RPC speaks in.

Everything hard about highvisor lives behind this seam (see docs/01-architecture
.md). The engine only ever talks to a PlatformBackend; the Windows and (future)
macOS backends are the only OS-aware code. Keep this file OS-neutral.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Target:
    """A controllable thing — an app window. ``id`` is a stable-ish string the
    client passes back to address it (we use ``hwnd:0x....`` on Windows)."""
    id: str
    kind: str            # "window"
    pid: int
    title: str
    class_name: str
    x: int
    y: int
    w: int
    h: int
    focused: bool
    visible: bool
    path: str = ""        # app bundle/executable path, when the OS exposes it —
                          # so highvisor can report how to relaunch what it sees

    def to_dict(self):
        return self.__dict__.copy()


@dataclass
class Element:
    """A node from the accessibility tree (``inspect``)."""
    role: str
    name: str = ""
    value: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    actions: List[str] = field(default_factory=list)
    children: List["Element"] = field(default_factory=list)

    def to_dict(self):
        return {
            "role": self.role, "name": self.name, "value": self.value,
            "bounds": [self.x, self.y, self.w, self.h],
            "actions": self.actions,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class ActionResult:
    """Result of an act. ``tier`` records WHICH delivery path actually worked, so
    the caller (and we) learn each app's real capabilities:
        1 = accessibility action (UIA pattern / AX action)  — background, semantic
        2 = window message post (WM_SETTEXT / PostMessage)   — background, syntactic
        3 = cooperative hook (target polls our channel)      — background, opt-in
        4 = activate + global input (SendInput / CGEvent)    — steals focus
    """
    ok: bool
    tier: Optional[int] = None
    detail: str = ""
    error: Optional[str] = None
    #: Structured observations the caller may branch on. `detail` is prose for a human;
    #: anything a CALLER needs to act upon belongs here, because a fact buried in a
    #: sentence gets read straight past — `activate` reported "frontmost unconfirmed"
    #: in its detail for months and every consumer, including me, checked only `ok`.
    data: Optional[dict] = None

    def to_dict(self):
        d = {"ok": self.ok, "tier": self.tier,
             "detail": self.detail, "error": self.error}
        if self.data:
            d.update(self.data)
        return d

    @classmethod
    def fail(cls, error):
        return cls(ok=False, tier=None, error=str(error))


class BackendError(Exception):
    """Raised for addressable, client-facing failures (bad target, etc.)."""


# Named half/quadrant zones of the primary display, resolved against its physical
# size. This is the vocabulary the Ersatz layout uses ("Godot top-right over the
# slurped program bottom-right"). OS-neutral on purpose — the engine computes the
# rect from the backend's screen_size() and hands the backend plain pixels.
ZONES = ("full", "left", "right", "top", "bottom",
         "top-left", "top-right", "bottom-left", "bottom-right")


def zone_rect(zone: str, screen_w: int, screen_h: int):
    """Return (x, y, w, h) in physical pixels for a named ZONES entry."""
    hw, hh = screen_w // 2, screen_h // 2
    table = {
        "full":         (0, 0, screen_w, screen_h),
        "left":         (0, 0, hw, screen_h),
        "right":        (hw, 0, hw, screen_h),
        "top":          (0, 0, screen_w, hh),
        "bottom":       (0, hh, screen_w, hh),
        "top-left":     (0, 0, hw, hh),
        "top-right":    (hw, 0, hw, hh),
        "bottom-left":  (0, hh, hw, hh),
        "bottom-right": (hw, hh, hw, hh),
    }
    if zone not in table:
        raise BackendError("unknown zone %r (known: %s)" % (zone, ", ".join(ZONES)))
    return table[zone]


class PlatformBackend:
    """Abstract per-OS backend. Subclasses implement all methods. Every method
    runs on the engine's single worker thread (see engine.py), so implementations
    need not be thread-safe, but MUST be reasonably fast / non-blocking."""

    name = "abstract"

    def thread_init(self):
        """Called once on the worker thread before any op (e.g. COM init)."""

    def list_targets(self) -> List[Target]:
        raise NotImplementedError

    def launch(self, spec: str, args=None) -> "ActionResult":
        """Start a program. ``spec`` is OS-interpreted: a URL scheme
        (``steam://rungameid/...``), an app path/bundle, or an app name. ``args``,
        if given, is forwarded to the program's argv."""
        raise NotImplementedError

    def drag(self, target: str, x1: int, y1: int, x2: int, y2: int,
             button: str = "left", steps: int = 12,
             modifiers: Optional[str] = None, hold: float = 0.08) -> ActionResult:
        """Press at (x1,y1), move through intermediate points, release at (x2,y2),
        window-relative. Distinct from two clicks: selection rectangles are built
        from the moves BETWEEN press and release."""
        raise NotImplementedError

    def screenshot(self, target: Optional[str], native: bool = False) -> bytes:
        """PNG bytes of ``target`` (a window ref) or the screen if target is
        None/"screen". Must work for an UNFOCUSED window. ``native`` requests a
        true-backing-scale capture via the platform's non-deprecated engine where
        one exists (macOS: ScreenCaptureKit); backends may ignore it."""
        raise NotImplementedError

    def activate(self, target: str) -> ActionResult:
        raise NotImplementedError

    def text(self, target: str, text: str) -> ActionResult:
        """Set/insert text into the target's editable element, unfocused if
        possible (tier ladder)."""
        raise NotImplementedError

    def key(self, target: str, keys: str, focus: bool = False) -> ActionResult:
        """Deliver a keystroke (named key like 'Return', a single char, or a
        combo) to the target, unfocused if possible (tier ladder). ``focus=True``
        forces the focus-stealing path for apps that ignore background keys
        (Unity/other engines)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ MODIFIERS
    # READ THIS BEFORE IMPLEMENTING click/drag/scroll ON A NEW BACKEND.
    #
    # A modifier must be REALLY HELD around the event. Setting a modifier bit on the event
    # itself is not enough — and both existing backends learned that the hard way, separately,
    # within a day of each other:
    #
    #   macOS  — CGEventSetFlags(kCGEventFlagMaskControl) on a click or a wheel reaches Godot
    #            with ctrl_pressed FALSE. Ctrl+wheel never opened Raves' state-graph panel and
    #            Ctrl+click never fired its cell inspector, while unmodified clicks worked
    #            perfectly throughout. Fixed by pressing the real key around the event
    #            (_hold_mods/_release_mods in darwin.py).
    #   Windows — the same symptom; fixed by emitting BOTH the VK and the scancode form,
    #            because Godot reads one and Unity the other.
    #
    # Two platforms, two mechanisms, one lesson: the app is not reading your flag, it is
    # reading the keyboard. Whatever your OS calls "held", do that.
    #
    # And whatever you do, RELEASE IN A `finally`. An orphaned modifier makes every later
    # synthetic key arrive modified and silently no-op, survives app restarts, and is close to
    # undiagnosable from the app side — a full day went into it once, blamed on the app.
    #
    # `modifiers` is a string; accept BOTH separators ("cmd,ctrl" and "ctrl+alt+shift"). The
    # two lines grew different spellings and unifying the name without the separator would
    # just move the bug.

    def click(self, target: str, x: int, y: int, button: str = "left",
              double: bool = False, hover: bool = False,
              modifiers: Optional[str] = None) -> ActionResult:
        """Click at (x, y) given RELATIVE to the target window's top-left, in the
        window's coordinate units. Synthetic mouse events reach many apps that
        drop synthetic keys (Unity games); this activates the window first.
        ``hover``: post a real mouseMoved before the click, for UIs that activate the
        item under the reported mouse position rather than the OS cursor (e.g. Qud's
        legacy popups). Off by default — a pre-move breaks direct world-cell clicks.
        Backends that don't implement hover ignore it; the engine only forwards it
        when explicitly requested, so it never reaches a backend that lacks the arg."""
        raise NotImplementedError

    def scroll(self, target: str, x: int, y: int, dy: int = 1, dx: int = 0,
               modifiers: str = "") -> ActionResult:
        """Wheel event at a window point. ``dy`` is in LINES, positive = up/away;
        ``modifiers`` is HELD, not flagged — see the MODIFIERS note above; an earlier version
        of this docstring asserted the opposite and it was measured wrong on both platforms.

        Separate from ``click`` because a wheel is not a button: it carries no position
        of its own and lands wherever the OS cursor is, so an implementation must warp
        first. Added for Raves' Ctrl+wheel state-graph panel, which no other op could
        reach — and a gesture that can only be tested by hand is a gesture that stops
        being tested."""
        return ActionResult.fail("scroll not implemented on this backend")

    def inspect(self, target: str, depth: int = 3) -> Element:
        raise NotImplementedError

    def ocr(self, target: str) -> dict:
        """Recognize text in the target window's capture. Returns
        {w, h, boxes:[{text, bbox:[x,y,w,h] in capture pixels}]}. The escape hatch
        for AX-opaque apps (web/Electron/canvas UIs) — read what you can't inspect."""
        raise NotImplementedError

    def screen_size(self):
        """Return (width, height) of the primary display in physical pixels."""
        raise NotImplementedError

    def displays(self):
        """List every display as ``{id, x, y, w, h, main}`` in the same point
        coordinate space that ``move`` uses (so a secondary monitor reports its
        real origin offset). Default: a single display at the origin derived from
        ``screen_size()`` — backends with multi-display awareness override this."""
        w, h = self.screen_size()
        return [{"id": 0, "x": 0, "y": 0, "w": w, "h": h, "main": True}]

    def move(self, target: str, x: int, y: int, w: int, h: int,
             topmost: Optional[bool] = None) -> ActionResult:
        """Position + size ``target`` to a physical-pixel rect. ``topmost`` is
        tri-state: ``True`` pins it above non-topmost windows (used to pin an
        Ersatz overlay), ``False`` explicitly clears the topmost bit, ``None``
        (default) just raises without touching the topmost state."""
        raise NotImplementedError
