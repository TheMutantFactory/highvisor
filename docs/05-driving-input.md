# 05 — Driving input: synthetic keyboard & mouse for background macOS & Windows apps

How highvisor delivers input to a target, the tier ladder it climbs, and the
hard-won detail that makes clicks land in engines (Unity/Qud) that drop everything
else. Dated 2026-07-28, verified on macOS against Caves of Qud (Unity, build
2.0.211.59) and Godot apps.

## The tier ladder (recap)

Every act reports **the path highvisor used** (`ActionResult.tier`), so we learn each app's real
capabilities. `ok:true` means the backend **completed that delivery attempt without an API-level error** —
it is *not* proof the target reacted. For semantic AX/UIA actions (tier 1) that's strong evidence; for raw
mouse/key posting (tiers 2/4) confirm the effect with a screenshot, an AX value, OCR, or a target-specific
state probe. The tiers:

1. **accessibility action** — UIA pattern / AX `AXPress`/`AXSetValue`. Background,
   semantic, the only *reliable* unfocused path. Requires the app to expose an a11y
   tree.
2. **window message post** — `PostMessage` (Win) / `CGEventPostToPid` (mac).
   Background, syntactic, per-app-flaky.
3. **cooperative hook** — the app polls our command channel (the generalized
   `godot_cmd` trick). Background, opt-in, for targets we own the source of.
4. **activate + global input** — focus-stealing synthetic keyboard/mouse. Last
   resort, but the only thing that works for many closed-source engines.

## Keyboard

- `key(target, keys)` posts at **tier 2** (`CGEventPostToPid` / `PostMessage`).
- `key(target, keys, focus=True)` — `hv key … --focus` — is **tier 4**: activate,
  then post to the HID event tap (`kCGHIDEventTap`) / `SendKeys`. Use it for apps
  that ignore background key posts.

**Finding: Unity (Caves of Qud) ignores synthetic keyboard entirely** — even tier
4 with a `kCGEventSourceStateHIDSystemState` source. Qud also exposes **no
accessibility tree** for its menu (only window-control buttons), so there is *no*
key or AX path into its menus. Qud only takes keys via the **mod's in-game command
injection** (`Keyboard.PushCommand`), which does not exist pre-game (menus /
character creation).

## Mouse — and the click-state gotcha

`click(target, x, y, button, double)` — `hv click <target> <x> <y>` — coordinates
are **window-relative** (added to the window's screen origin).

> **`click` steals focus — it is not a background action.** It calls `activate(target)` first (a click on
> a background app should focus it), so it is a **tier-4** path. Background *mouse* control would need a
> semantic AX action or a cooperative target path; coordinate clicking is not that. (Background *keyboard*
> to some apps works via `key` tier 2; mouse does not.)

It:

1. **Warps the real OS cursor** to the point (`CGWarpMouseCursorPosition`). Unity
   reads the *actual* cursor position for hover, so the warp is what makes the
   hover highlight follow — a synthetic move event alone does not.
2. Posts a **bare `LeftMouseDown` / `LeftMouseUp` pair** from a
   `kCGEventSourceStateHIDSystemState` source to `kCGHIDEventTap`.

**The gotcha (this is the whole point of the doc):** the click only registers as a
real selection in Unity if the event is *minimal*:

- **Do NOT set `kCGMouseEventClickState`.** Setting it (even to 1) makes Qud drop
  the click — it highlights on hover but never activates.
- **Do NOT post a pre-move `kCGEventMouseMoved`.** The warp already positions the
  cursor; an extra move event also causes Qud to reject the click.

With the field set / pre-move posted: hover works, selection silently fails. With a
bare down/up: the click selects. This matches a known-good auto-clicker exactly —
[othyn/macos-auto-clicker](https://github.com/othyn/macos-auto-clicker)
(`AutoClickSimulator.swift`): HID-system-state source, `.cghidEventTap`, plain
`CGEvent(mouseEventSource:mouseType:mouseCursorPosition:mouseButton:)` down then up,
no click-state, no move. Verified: warp to a menu item + bare click **opened Qud's
Options screen** — a genuine activation.

Windows equivalent: `SetCursorPos` + `mouse_event(down|up)` after `activate`.

### Coordinates are points — but a screenshot may not be

`click` x/y are window **points** (logical), added to the window origin. A `shot` PNG,
though, comes back at the window's **backing scale**, which is not always 1×: a full-size
Raves/Qud window returned pixel-for-point (1:1) this session, but a *smaller* window came
back at **2×** (shot px = 2× points). So don't read coordinates straight off a screenshot
assuming px == points — compare the `shot` dimensions to the window's `ls` W×H and divide
by the ratio before clicking. Driving targets at **full window size** kept it 1:1 and clicks
landed first try; a mis-scaled coordinate silently hits the wrong control.

## Practical guidance (which tier for which target)

| target | keyboard | mouse | notes |
|---|---|---|---|
| Native AppKit / standard apps | tier 1–2, or `--focus` | click works | a11y usually present |
| Qud **Unity UI / title menu** | **none works** | **bare first, escalate to `--hover`** | no AX tree; keys dropped. Bare has worked, but the title-menu items have also needed `--hover` when a bare click didn't move the highlight — verify per session |
| Qud **legacy console popup** (Load-game picker, in-game ☰, "[Space]" prompt) | none works | **`--hover`** | reads `Input.mousePosition`; needs the pre-move a bare warp doesn't provide |
| Qud **in-game world cell** | none works | **bare click, NEVER `--hover`** | a pre-move makes Qud hover-but-never-*select* the tile |
| Godot apps (our own) | prefer the mod/command channel (tier 3) | click works | cooperative hook is cleanest |

The tension between "no pre-move" (above) and "`--hover` was required" is real and **surface-specific**, not
a contradiction: bare wins for plain Unity buttons and world cells (a pre-move gets rejected), while
`--hover` — a real `mouseMoved` before the bare click — is what the legacy popups (and sometimes the title
menu) need. When unsure, try bare, screenshot, and escalate to `--hover` only if the highlight didn't move.

**Rule of thumb for closed engines: drive them by mouse, not keys.** Position with a cursor warp and start
with a **bare** down/up. Add `--hover` (a real mouse-moved) **only** for a surface whose readback proves it
needs one (legacy popups do; world cells reject it); **never add click-state metadata for Qud.** Posting the
event is not proof it registered — screenshot and confirm.

## Recipe: Caves of Qud, title → in-game

Qud's bridge (the mod socket on **48710**) only opens once a save is loaded, so an
automated loop has to walk the pre-game menus by mouse first:

1. `shot` the Qud window and confirm the title menu is up.
2. `click --hover` **Continue**. Bare clicks drive Unity *buttons*, but the title-menu
   *items* did not reliably select on a bare click this session (the highlight never moved
   to the clicked item) — so **escalate to `--hover`** if a bare click leaves the selection
   unchanged.
3. The **Load Game** picker (a legacy console popup) appears — `click --hover` the save row.
4. Poll `nc -z 127.0.0.1 48710` until it opens = in-game. From here, prefer the mod's
   command channel (tier 3) over more clicking.

Verified end-to-end this session: title → Continue → Load picker → save row → bridge up,
entirely via `hv click --hover`.


## Restart readiness: a window is not an app (2026-08-05)

`restart` used to return as soon as a window with the right title existed. For a Godot app that is
about a second after launch — before settings load, before the bridge connects, before the app has
reported a scene — so "restart succeeded" told a caller nothing about whether driving it would
work. It now additionally waits for the app's own state file to carry a write NEWER than the
launch, and reports `reporting: true|false`.

Newer-than-launch is the test, not freshness: the dying process's last write is only a second or
two old and looks perfectly fresh, so freshness alone cannot distinguish the new process's first
word from the corpse's last.

For Qud this is the more valuable half — its window appears well before the mod has compiled and
started its heartbeat, and `loadsave` needs the mod, not the window.

**Honest scope.** This was written to fix an intermittent `restart → goto → assert` failure, and it
is NOT demonstrated to do so. By the time it existed the failure had stopped reproducing: six
trials, warm and cold (i.e. straight after a rebuild), with the gate both enabled and disabled, all
passed — and no ghost report was observable either (Raves' state file flips to the new process
essentially immediately). The likeliest cause of that flakiness was the popup re-announce churn
fixed in raves-of-qud `cd62ff8`, which had Qud dumping a GPU texture, writing a PNG and deleting a
file twice a second. The gate is kept because the postcondition is strictly better and costs about
0.7s, not because it is a proven fix.


## goto tracing: the flight recorder (2026-08-05)

Every `goto` run appends a record to `~/.config/highvisor/goto-trace.jsonl` (bounded ring, ~400
runs): the state it **steered by** on entry, each step's outcome, and the state it left behind.
Read it with `hv trace [n]`.

It exists because a failing goto is normally only diagnosable after the fact, and the return value
cannot tell a real success from a trivial one — a recipe that reports `ok` because the app was
"already at" a node it had in fact just left looks identical to one that drove there. In the trace
they do not: the `entry` field is the belief the run acted on.

It earned itself within a minute of existing. Three consecutive failures all read:

    FAIL raves in_game   title -> title   error: text 'continue' not on screen (16 ocr lines)

An OCR of the window showed why: Raves was on the **LOAD GAME** screen (save picker), while its
state file still reported `scene=title`. So every recipe that assumes the title MENU clicks for a
"Continue" that is not there, fails, and leaves the app exactly where it was — which is why retries
never helped and only a restart appeared to fix it.

The fix belongs in the app, per the standing rule: **when detection is wrong, teach the app to
report the scene.** `LoadGameScreen` needs a `UiState.set_scene` call.


## Leaving a live game: `goto title` is reversible now (2026-08-05)

Neither app's `title` recipe could exit a running game — it self-healed menus and popups only —
so `goto title` from `in_game` failed and left the game up, and every recipe beginning
`{goto: title}` inherited that. The goto trace is what made it visible.

Raves' recipe now runs Qud's own quit and answers the confirms on the RAVES window, because the
popup mirror puts them there:

    dismiss {popup: message, key: Escape}          cancel a stray modal first
    dismiss {scene: in_game, command: CmdQuit}     Qud's own quit command
    dismiss {popup: message, key: space}           "Are you sure you want to quit?" -> Yes
    dismiss {popup: message, keys: [Right, space]} "Do you want to save first?"     -> NO

**Answering No is deliberate and load-bearing.** Yes would overwrite the fixture save with
whatever state the harness had driven the character into — quietly destroying the reference every
parity capture is measured against. Verified: after a full round trip the save's timestamp is
unchanged.

Three primitives were added to `dismiss` to express this, each because the flow proved a gap:

| addition | why |
|---|---|
| `popup:` condition | leaving a game means answering a chain of confirms while the SCENE never changes, so scene alone cannot tell the steps apart |
| `command:` action | a named QUD command through the mod, so the flow starts through Qud's own command path instead of guessed keys |
| `keys:` sequence | some keys move a selection INSIDE a modal rather than answering it — the Right that shifts a confirm from Yes to No changes nothing observable, and verifying after it fails a step that worked |

The postcondition for a dismiss is now that the app's (scene, popup) PAIR changed, which is what
lets one branch serve all three shapes: closing a screen moves the scene, raising a confirm moves
the popup, answering one moves it back.

Qud's own recipe does the same thing WITHOUT Raves, and without a single key:

    dismiss {scene: play, command: CmdQuit, answers: [Yes, No]}

`CmdQuit` goes through Qud's own command path, and each confirm is answered by BUTTON through
the mod (`popup / action:button / btn:`), which dismisses the popup it announced. That matters
because Qud's modern UI ignores OS-synthesized keys outright — the constraint that made this look
undoable. Qud publishes no popup state of its own, so the prompts cannot be conditioned
individually the way Raves' can; instead the chain is sent blind, which is safe because answering
when nothing is up is a no-op the mod logs and discards.

Verified for both apps: `goto title` from a live game passes on repeated cycles, `title <-> in_game`
round-trips, and the fixture save's timestamp is unchanged throughout. `_load_save`'s restart path
is untouched — it is still the right move when the goal is a CLEAN process, not just the title.


## The Qud equipment screen: an opener, a ghost, and ten dead clicks (2026-08-05)

`goto qud status_equipment` never opened anything. Its recipe carried the note "open the status
screen first (opener recipe TBD)" and merely ASSERTED the screen was already up, so it failed
whenever it was not. It now chains `status_screens` (which opens by CLICKING the HUD button --
Qud's own `e` opens the screen but is not a toggle, so it cannot be used to reach a known state)
and then clicks the Equipment tab.

Two things it uncovered, both of which had been costing time for a while:

**The ghost modal.** `UIManager.popupMessages` is a free pool, and a RELEASED copy still looks
live -- so the mod can report `scene=PopupMessage` with nothing on screen, and Qud swallows every
key while it believes a modal is up. That is what made the equipment key look broken for a whole
session; the key was fine. `uiback` clears it (verified), so both the equipment and title recipes
now self-heal `PopupMessage` as well as `DynamicPopupMessage`. Driving popups over the bridge is
what leaves them, so any scripted popup work should expect one.

**Ten dead click steps.** Every Qud status-tab recipe wrote its click as
`{"click": {"window": …, "x": …, "y": …}}`, but the engine reads `{"click"|"click_hover": [x,y],
"window": …}` -- so `window` resolved to None and the step failed EVERY time it ran, in all eight
tab recipes plus the status_screens opener and one Raves recipe. Nobody noticed because the
screen was usually already open and no recipe asserted afterwards. The goto trace surfaced it in
one line: `error: no window None`.

Verified from a clean in-game state: closed -> open on the Equipment tab, 7 steps, no failures.

## The graph refactor: routes are DERIVED now (2026-08-06)

Every section above this one is written in the language of *recipes* — a node's `goto[app]` step
list, driven from a known base. That model is gone. A node no longer stores how to reach it;
the tree stores **transitions**, and the route is searched at call time.

A transition says: *from* these states, doing these steps, you arrive at *this* state, at *this*
cost.

```json
{"app": "qud", "from": {"within": "status_screens"}, "to": "in_game",
 "steps": [{"bridge": "uiback"}], "verify": {"node": "in_game"}, "timeout": 12}
```

`from` is a node id, a list of them, `{"within": <node>}` (that node or any descendant), or `"*"`
(anywhere). `to` is where you end up. `verify` defaults to `{"node": to}` and is not optional in
spirit: an edge that does not check its own arrival lets a route continue from a state it merely
assumes, and the failure then surfaces several steps later somewhere unrelated.

### What this fixed that the recipes could not

**Routes from anywhere.** Every recipe opened with `{"goto": "title"}` or `{"goto": "in_game"}`,
so a start the author had not anticipated was not re-routed — it drove the base's recipe from the
wrong screen and failed a step that was never wrong. That is the whole "goto needs retries"
folklore. The planner starts from the state the app is *detected* in. `off` and `unknown` are
real planner states for exactly this reason: "the window is up and nothing matched" now has a way
out instead of being a dead end.

**Shared prefixes.** All eight Qud status tabs repeated two ghost-modal dismisses plus a chain
step; all eight Raves tabs repeated an eight-scene dismiss. Fixing one meant fixing eight, and
they drifted. Both are now one edge each — `{"within": "status_screens"} → in_game` — inserted by
the planner when, and only when, the route needs it. **Raves tab-to-tab used to alternate
pass/fail** (the chain step routed through `title`, which from a status screen dismissed it,
landed in the game, and failed its own assert); it is now two edges, Escape then the tab key.

**Cost is a number.** `costs` in gametree.json prices a step by how much we want to AVOID it, not
by how long it takes: bridge commands are near-free and never miss, `click_text` is dear because
OCR is the one class that goes flaky *and a miss does not fail cleanly — it clicks the wrong
thing*, launch is slow, restart is 120 and therefore last. The planner minimises that sum, so it
prefers first-party moves without anyone remembering to.

**Impossible targets fail before anything is touched.** `plan.route` returns a reason that names
which way the graph is broken — nothing ENTERS the goal, nothing LEAVES the start, or the two are
in different components — instead of discovering it halfway through a driven run.

**Re-planning.** When an edge fails, the engine re-reads the state; if the app MOVED somewhere
unexpected it plans again from there (twice, then it gives up — if it did not move, re-planning
would return the same route and loop). A fixed sequence had no way to express this.

**The tombstone became reachable.** `summary` (death → GameSummaryScreen) had no recipe at all;
its note said "injected Esc is IGNORED here — a restart is the reliable exit", which was true and
unusable. With a universal `"from": "*"` restart edge at cost 120, `summary → in_game` is a
route. That edge is also the guarantee that **there is always a route**, and the fact that the
planner *picks* it is the signal that a real exit edge is missing.

### Why A* with a zero heuristic

`plan.route` is A*; its default heuristic is zero, which makes it exactly Dijkstra. That is
considered, not a stub. The obvious heuristic — hops in the containment tree — is **inadmissible**
here: `status_skills → status_journal` is two tree hops but ONE transition (a `statustab` bridge
call), so it overestimates, and A* would return a costlier route while looking like it worked.
At ~66 states and ~49 edges the search is microseconds either way, so optimality is worth more
than pruning.

### Ghost modals are a guard, not an edge

A pooled `PopupMessage` that reports live with nothing on screen is not a place you can BE — it
is a condition that can be true anywhere, and it eats every key while it is. As a node it would
double the graph; as an edge it would need a self-loop, which shortest-path search cannot use. So
it is `preflight`: check, clear, re-read, *then* plan. One declaration replaced sixteen
copy-pasted dismiss steps.

### Surfaces

- `hv plan <app> <node> [--from <state>]` — the route `hv goto` would take, **without driving
  anything**. `--from` takes a node id, `off` or `unknown`, so a route can be checked for a
  screen we are not on, with nothing running.
- `hv goto` prints the route it chose before the step detail.
- `python3 tools/selftest_plan.py` — stdlib only, no daemon, no apps. Checks the planner against
  a synthetic tree, then checks the REAL graph: every endpoint is a real node, and **every target
  is reachable from every state we might be found in**. Under the recipe model that question was
  answerable only by driving both apps and watching, which is how a broken route survived until a
  capture run tripped over it.

Legacy `goto[app]` recipes still run for any node the graph cannot reach, and the result says
which driver was used — a node driven by a recipe is a node whose transitions nobody has written
yet, and that should be visible rather than silent.

### Two live findings from the first planned runs (2026-08-06)

Both were pre-existing and both had been invisible, which is the argument for `hv plan` and
for edges that verify.

**`click_text` was reading a stale frame.** `goto qud in_game` from the title reported
`text 'continue' not on screen (14 ocr lines)` while looking straight at Continue. Qud does not
repaint while unfocused (the ~2s settle documented in CLAUDE.md); `activate` waits 0.6s and
`click_text` then OCR'd **once**, so it read the screen Qud had been on before — the 14 lines
were the in-game HUD. It now polls the OCR to a deadline (default 6s) instead of snapshotting,
which also covers a menu still animating in and costs nothing on the normal path. Deliberately
not fixed by sleeping longer after `activate`: that taxes every step to serve the one that reads
pixels. The error now prints the lines it *did* see, so the next stale frame is obvious.

**The top-row roulette was still in a recipe.** With the OCR fixed, `title → in_game` failed at
the next step: a blind `{"click_hover": [900, 190]}` labelled "top save row". That is precisely
what `hv loadsave` was written to end — and it had been sitting inside the one recipe nobody had
re-driven since. The edge now uses a `{"load_save": {"row": 0}}` step, which goes through the
mod's own `loadsave {id}` bridge command with the id resolved from **disk** metadata: exact
match, no coordinates, no OCR, and it opens Qud's picker itself. `{"row": n}` rather than a
character name so the graph does not hardcode a save.

Verified live, all planned, no fallbacks: Raves tab→tab ×4 consecutively (the case that used to
alternate pass/fail), Raves tab→in_game, Qud in_game→status_screens→tab, Qud tab→tab as a single
`statustab` call, Qud status→title (`uiback` then `CmdQuit` + two answers), Qud title→in_game,
Raves title→in_game.

### The recipes are gone

With the graph covering every node that had one, all 20 `goto[app]` recipes were deleted. They
were already unreachable — the fallback can only fire where the planner has no route — and two
descriptions of the same move is the drift this refactor existed to end. Every note worth
keeping was carried into the transitions verbatim. `selftest_plan.py` now fails if a recipe
reappears for a node the graph can already reach. The fallback **code** stays: adding a recipe
for a node the graph cannot yet reach must still work.

## The screens with "no way out" (2026-08-09)

Two Qud screens got a reputation for being inescapable, and it was earned honestly: an
HID Escape, a click, and a second press of the button that opened them all leave the
**Looker** and the **Book** (an item's "show effects") exactly where they were. So does
`hv click_text`, since neither carries a caption to aim at. The conclusion drawn from that
— *this screen has no exit, restart Qud* — cost a session and a save reload.

It was wrong, and the measurement that settles it is one line long:

| exit attempt | Looker | Book |
|---|---|---|
| `hv key qud escape --focus` (OS/HID) | no | no |
| `hv bridge key key=escape` (the mod's LEGACY key queue) | yes | **no** |
| `hv back` (the mod's `uiback`) | **yes** | **yes** |

The Book row is the interesting one. It looks legacy — `GameManager` pushes the game view
`Book`, which is what `hv state` reports — but with `Options.ModernUI` on it is the modern
`BookScreen` window (the sampler reads `window=BookScreen` behind `view=Book`). So it reads
neither Unity's OS keys nor `ConsoleLib.Console.Keyboard`'s queue, and only the first-party
cancel reaches it. **A legacy-looking view name is not evidence of a legacy screen; the
sampled window is.**

`uiback` closes both, by different rungs of its own ladder: the Book through
`FireInputButtonEvent(CancelButton)`, the Looker through the last rung's queue-injected
`Cancel` FrameCommand — which works because `Keyboard.metaMousecommands` maps `Meta:Cancel`
to `Keys.Escape`, and the Looker's `getvk` loop is waiting for exactly that.

### The real gap was the graph, not the exit

Neither screen was a node, so neither had an edge, so `hv goto qud in_game` answered *"the
only route to in_game RESTARTS qud (it is the cheapest route)"*. CLAUDE.md already says the
planner picking `restart` is the signal that a real exit edge is missing — this is what it
looks like when nobody reads the signal. Three things closed it:

- **`look`** (the Looker) and **`book`** are nodes now, so `hv state` names them instead of
  answering `running · unknown screen`. Neither can be claimed by `in_game`: both park the
  turn thread, so the mod publishes no snapshot and `game_live` reads false with a live game
  behind it.
- One edge, `["look","book"] -> in_game`, one `uiback`. No `exact` on the verify — the Book
  can be opened from a status screen, so landing there is a legitimate arrival, and
  `_drive_route` attaches `not_within` to a climbing edge by itself.
- **`unknown -> in_game`, also one `uiback`** — the part that matters for the screen nobody
  has modelled yet. Cost 1 against restart's 120, gated on `port_open`, and it cannot lie
  about arriving: `unknown` is not inside `in_game`, so the default verify is a real check
  and a failure re-plans onto the restart it would have taken anyway.

Verified live, all three, from the screens themselves: `book -> in_game` and `look ->
in_game` each one bridge call, and the generic net proved by blinding the Book's detector so
the screen really was unmatched — `unknown -> in_game (1) [cost 1]`, arrived.
