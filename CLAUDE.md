# Working notes for Claude (and future humans)

highvisor = the localhost desktop-UI supervisor: observe + drive native apps (focused or NOT)
through one daemon. The raves-of-qud parity work is its first consumer; the design goal is to be
reusable for ANY app pair. Cockpit web UI on **:48721**, daemon RPC on **48720**.

## THE PRIME RULE — drive apps through highvisor, never by hand

Manual `open`, raw AppleScript window moves, and hand-rolled click seances are how sessions
thrash. The `hv` commands are the interface; when one is missing or broken, **fix highvisor**
(that's this repo) rather than working around it — the workaround dies with the session, the
fix compounds. Concretely:

| instead of… | use |
|---|---|
| `open <app>.app` / steam URL | `hv launch raves` (pair), `hv launch raves_solo` / `qud_solo`, or the cockpit ▶ buttons |
| AppleScript / manual window moves | `hv move` (readback-verified), cockpit slot buttons |
| screenshot-then-guess "what screen is it on?" | `hv state` (first-party scene reports), `hv probe` |
| click-drive to a known screen by hand | `hv goto <app> <node>` (planned route over the transition graph) |
| guessing what a goto will do | `hv plan <app> <node> [--from <state>]` — the route, driving nothing |
| sleep-and-hope waiting for a state | `hv assert --app qud --node in_game --timeout 20` |
| pkill/osascript restart seances | `hv restart qud` / `hv restart raves` (kills ALL instances incl. duplicates, relaunches, waits for the app to REPORT) |
| blind top-row Continue clicks | `hv loadsave <name>` (row computed from DISK metadata — `hv saves` lists them, no game launch) |

The daemon **self-restarts** (2026-08-03): a source watcher re-execs it when any highvisor `.py`
changes (edit → it picks itself up in ~2s), and the launchd KeepAlive agent is INSTALLED
(`com.highvisor.daemon`) so a crash respawns it — verified by `kill -9`: new pid, port listening
~5s later. Logs: `~/Library/Logs/highvisor.log`.

**The agent runs Highvisor.app, and that is not cosmetic.** macOS attributes Screen Recording to
a *responsible process*: a daemon started from a terminal inherits the terminal's grant, the
identical binary started by launchd does not. Measured A/B, same venv interpreter: launchd-spawned
saw every window title as blank, shell-spawned read them fine — and the symptom surfaces three
layers away as `no window for app 'raves'` or `text 'continue' not on screen` while looking
straight at Continue. `tools/make_app.sh` builds `build/Highvisor.app`: an ad-hoc-signed bundle
whose tiny C stub **forks** the venv python and stays alive as its parent (an `execv` stub would
defeat the point — the process image would become the interpreter again, and responsibility flows
parent→child). Grant "Highvisor" Screen Recording ONCE and it survives venv rebuilds, Python
upgrades and every source edit. `hv install-daemon` builds it if missing, kills any manually-run
daemon (port clash), bootstraps, then **verifies BOTH grants** and prints the remedy if either
is missing. `hv ls` warns too: several windows and not one title is the signature.

**TWO grants, not one.** *Screen Recording* covers window titles, captures and OCR. **Accessibility** covers synthetic INPUT (click/key/scroll/text) and the AX tree — and its
absence is the nastier one: `CGEventPost` does **not** fail without it, so every click and
keypress returned `ok: true` and went nowhere. That cost an hour of suspecting the app while
Raves' title menu ignored everything and screenshots worked perfectly. Those ops now check
`AXIsProcessTrusted()` first and fail loudly. **If input is being ignored, check the grant
before the app.**

**The two grants resolve to DIFFERENT identities — this is the part that wastes an evening.**
Screen Recording resolves via the RESPONSIBLE process, so granting `Highvisor.app` works.
Accessibility is keyed to the CALLING process's own signed identity — and the framework
python's `bin/python3.9` re-execs into `Resources/Python.app`, so the daemon really runs as
`com.apple.python3` no matter what launched it. Granting Highvisor.app does **nothing** for
input. Run **`hv grant-input`**: it raises the system prompt from the daemon process, so macOS
names and lists the identity it actually wants (it shows up as "Python"). Narrower than what
highvisor relied on before — that grant came from Terminal, which covers everything anyone
runs there. **You cannot check that by PID or uptime** — `os.execv` replaces the
process image in place, so both are PRESERVED across a re-exec. A daemon showing hours of uptime
may well be running code you saved a minute ago. To tell, grep the daemon log for
`source changed: … — re-exec`, or call an op and look for behaviour only the new code has. (Cost
of learning this the other way: a wrong "the watcher is broken" conclusion, and a pointless manual
daemon restart.) If `nc -z 127.0.0.1 48720` fails now, the AGENT is down, not just the process —
`launchctl print gui/$(id -u)/com.highvisor.daemon` first, and check the log before
restarting anything by hand.
`webui/` static files still only need a browser reload.

## THE TIMESHARE GUARD — this machine is shared

Every focus/mouse-stealing op (activate, key --focus, click, text) runs inside a guard session
(`highvisor/guard.py`): it remembers the frontmost app + mouse position, plays a 3-ping audio
countdown before taking control (only when interrupting a NON-game app), restores focus + mouse
and plays a return cue when the session idles out (~8s) or hits the **20s hard cap**. Panic
channels: the cockpit 🛑 ABORT button, `hv abort`, `touch ~/.config/highvisor/ABORT`, or
**Ctrl+Opt+Cmd+H** anywhere — all release immediately and refuse control ops for 30s. Disable
for unattended runs: `touch ~/.config/highvisor/guard_off`.

## Map

| file | what |
|---|---|
| `highvisor/engine.py` | op dispatch + gamestate/gamego/assert logic |
| `highvisor/backends/darwin.py` | macOS backend (AX move/click/key, ScreenCaptureKit shots, OCR) |
| `highvisor/gametree.json` | THE canonical game state tree: per-node `detect` (how we know we're there) and `done` (1:1 score), plus the `transitions` graph, `costs` and `preflight`. Hot-reloads. |
| `highvisor/gametree.py` | tree loader + state evaluator (deepest match wins; detect OR-lists) |
| `highvisor/plan.py` | the route planner over `transitions` — pure data + search, no backend |
| `highvisor/cli.py` | the `hv` CLI (`~/bin/hv` wrapper runs it from any cwd) |
| `highvisor/webui/` | the cockpit (vanilla JS; served from disk, reload to pick up) |
| `~/.config/highvisor/launch.json` | machine-local launchers (`qud`, `qud_solo`, `raves`, `raves_solo`) |

## State detection — first-party beats OCR

The apps REPORT their own UI state into `~/Library/Application Support/RavesOfQud/`:
`raves_state.json` (Raves' UiState autoload) and `qud_state.json` (the mod's heartbeat thread),
each `{scene, …, ts}`, rewritten ~1-2s. The engine trusts a file while its mtime is fresh
(`STATE_FILE_TTL`), evaluates it as the `scene` signal in gametree detect conditions, and falls
back to OCR/port signals when stale. **When detection is wrong, teach the app to report the
scene** (add a `UiState.set_scene` call / extend the mod heartbeat) rather than piling on OCR
substrings.

**On a DEPTH TIE the more trustworthy signal wins, not tree order** (`gametree.TRUST`: tab >
scene > ocr > live > port). `title` and `in_game` are both depth 1; `title` used to carry a
`{"game_live": false}` fallback and the game_live probe is a 0.35s read on Qud's bridge, so a
busy Qud made `hv state` report "Title Screen  scene=play  via=live" while it was plainly
in-game. That was cosmetic until `gamego` began PLANNING from the detected state — a stray
"title" plans title->in_game, whose edge is `load_save`, i.e. reload the save over a running
game. Guarded by `python3 tools/selftest_evaluate.py`.

**A DEAD REPORTER MUST LOOK DEAD — no detector may rest on an inference alone** (2026-08-08).
That `{"game_live": false}` fallback is now GONE, because absence of bytes is not evidence: it is
satisfied by every menu screen, so any time the first-party report was stale or missing the tree
named the title on a guess. Measured twice — "Title Screen via=live" for 7 minutes while Qud sat
on the Modding Toolkit with the mod unloaded, and again on a parked Keybinds screen with a **live**
game behind it (a parked turn thread publishes no snapshot, so the probe is wrong about liveness
itself). Both confident, both wrong, and `gamego` plans from them.

- `signals["report"]` is `fresh | stale | foreign | absent` — the reason `_read_state_file`
  refused a file, which used to vanish into a bare `None` and made "refused report"
  indistinguishable from "matched no scene". Trees can condition on it; `hv state` shouts
  `!! report=stale (no first-party state; not guessing a screen)`.
- With no report and no live game the state is **`unknown` — "running, screen unknown"**, which
  the planner can route out of (`unknown -> title` via restart). That is the recovery the
  7-minute episode never got.
- **in_game keeps its `{"game_live": true}`, and the asymmetry is the point.** The mod publishes
  a snapshot on connect ONLY when a game is live, so BYTES imply a running game — a positive,
  near-sound inference whose worst case is naming `in_game` while we sit on one of its children.
  No bytes imply nothing. Keep positives, drop negatives.
- If an app ever ships without a reporter, **give it one** rather than restoring the fallback.

**A REPORT IS ONLY AS GOOD AS THE CODE BEHIND IT — a first-party "no" is not a measurement**
(2026-08-08). The rule above says a dead reporter must look dead. This is its sharper case: a
reporter that is *alive and answering* can still be reporting the absence of something it has
simply failed to render. Raves' `PopupOverlay` stopped building (a GDScript type error aborted
its builder), so `raves_state.json` faithfully published `no popup` — fresh, correct pid,
everything the reader checks — for popups that Qud was raising the whole time. `hv state`
showed no popup. `hv assert --popup` would have timed out. Both were right about Raves and
told you nothing about Qud, and a whole session was spent concluding "Qud raises no popups,
surviving a clean pair restart" from them.

- **Never let one app's report stand as evidence about the OTHER app.** For the popup channel
  the two independent sources are the mod's bridge frames (tap :48710 and read
  `type:"popup"`) and `hv shot CavesOfQud`. Either settles Qud's half in seconds.
- The same asymmetry as `game_live`: a report saying "X is present" is near-sound, a report
  saying "X is absent" is satisfied by every way the reporter could be broken. **Keep the
  positives, distrust the negatives** — including the ones that arrive on time.
- `hv goto qud quit_dialog` is still UNREACHABLE (no transition enters it). The Quit X on the
  title screen is a `click_hover` at window point **(50, 48)**, measured — it raises Qud's
  only MENU-level popup, which is the one case that proves a mirror works with no game and no
  player. Worth an edge, plus a return edge that answers `No`.

**Reports are PER-PROCESS.** A shared path has one writer per running instance: three live Raves
had `raves_state.json` cycling `in_game → status_tinkering → title` every 2s, so every read was a
coin flip — that, not a reporter bug, is why the tree "lied" and `hv goto raves in_game` needed
retries (with two windows up, `_find_win` can hand a recipe a different window than the one being
read). Raves now stamps `pid` and also writes `raves_state.<pid>.json`; the engine reads the
sidecar for the pid owning the window it is evaluating and REFUSES a shared file stamped with a
foreign pid — None (fall back to OCR/port) beats a confident wrong answer. Unstamped reports
(`qud_state.json`) read as before. `hv state` prints `!! N INSTANCES` and `hv goto` refuses to
drive while duplicates exist; `hv restart <app>` is the cure. Guarded by
`python3 tools/selftest_state_read.py` (stdlib only, no daemon, no apps — run it with any change
to the reader).

## gametree TRANSITIONS — routes are planned, not scripted (2026-08-06)

Nodes no longer store how to reach them. `gametree.json` carries a `transitions` list —
`{app, from, to, steps, cost?, verify?, timeout?}` — and `hv goto` SEARCHES a route from the
state the app is actually detected in (`plan.py`; A* with a zero heuristic, i.e. Dijkstra —
the obvious tree-distance heuristic is inadmissible, see the module docstring).

- `from` = a node id · a list · `{"within": node}` (that node **or any descendant**) · `"*"`.
  Two non-node states exist so "get me out of here" always has a start: **`off`** (no window)
  and **`unknown`** (window up, nothing matched).
- `verify` (default `{"node": to}`) runs after every edge. Non-negotiable: an edge that does
  not check its own arrival lets the route continue from a state it only assumes.
- `costs` prices a step by how much we want to AVOID it — bridge ~free, `click_text` dear
  (OCR is the flaky class, and a miss clicks the WRONG thing rather than failing), launch
  slow, **restart 120**. A `"*" → title` restart edge guarantees there is always a route; the
  planner PICKING it is the signal that a real exit edge is missing.
- **`preflight`** clears ghost modals before planning. A pooled `PopupMessage` is a condition
  on top of a state, not a state — as a node it doubles the graph, as an edge it needs a
  self-loop. One declaration replaced sixteen copy-pasted dismiss steps.
- On a failed edge the engine re-reads and **re-plans if the app moved** (twice, then gives
  up — if it did not move, the same route comes back and loops).

Steps (one vocabulary, shared with the legacy recipes): `{"launch": name}`, `{"restart":
app}`, `{"wait_window": label}`, `{"activate": label}`, `{"click_hover": [x,y], "window":
label}` (Unity menus need hover), `{"click_text": label, "window": label}`, `{"key": keys}`,
`{"command": name, "answers": [btn]}`, `{"bridge": name, "args": {...}}`, `{"dock": label}`,
`{"dismiss": {...}}`, `{"sleep": s}`, `{"assert": {...}}`. Coordinates are window points at
the standard 1920×1080 slots; re-measure if the layout changes.

Any step may carry **`{"os": "posix"|"nt"}`** and is SKIPPED where it does not match
`os.name`. That is the seam for a screen the two platforms genuinely have to reach
differently (`click_text` needs OCR, which only the darwin backend has) — both forms live on
the one edge and neither machine's evidence is thrown away. `selftest_plan.py` enforces the
rule that makes it safe: an edge must keep at least one ACTUATING step under *every* os, or
it is an edge that skips itself empty and reports ok.

- `hv plan <app> <node> [--from <state>]` — the route `hv goto` would take, driving NOTHING.
  With NO node it lists every reachable state and its cost (one call, not one per node).
- **`hv test [id] [--node N]`** — run a check REGISTERED IN THE TREE, or list them all. The
  caller names WHICH check; the command text lives in `gametree.json` under version control,
  next to the state it covers, so "run this node's check" can never become "run this string".
  Harness-wide checks sit at the tree's top level (`tests`); screen-specific ones on the node.
  Raves' state-graph panel AND the cockpit both render them as clickable `[T]` markers —
  same marker, same gesture, two surfaces.
- **The cockpit gates click-to-drive on REACHABILITY**, not on a stored recipe. It used to read
  `node.goto[app]`, so deleting the legacy recipes silently made every cell unclickable while
  the panel looked fine. If a UI ever needs "can this app get there?", ask `plan_route` — with
  no node it returns the cost to every reachable state in one call.
- `python3 tools/selftest_plan.py` — stdlib only, no daemon, no apps. Proves every target is
  reachable from every state we might be found in. Run it with any transition edit.
- The legacy `goto[app]` recipes are GONE — **all of them, in both apps** (the last, raves
  `blueprint_browser`, converted 2026-08-07). The fallback code stays, so adding a recipe for
  an unmodelled node still works, but `selftest_plan.py` fails if one reappears for a node the
  graph can already reach. Two recurring reasons a node resisted conversion, both worth
  recognising: it had **no detector** (the Map Editor's menu bar is a legacy RedShadow dialog —
  the mod reports the open dropdown as `tab` now), or its route started at a node **that app
  could not reach** (raves had no edge to `modding_toolkit`, so an edge out of it was
  unreachable from everywhere).
- `click_text` POLLS the OCR to a deadline: Qud does not repaint unfocused, so a single
  snapshot read the previous screen and "Continue is not on screen" meant "I am looking at
  a stale frame". Never load a save by clicking a row — `{"load_save": {"row": n}}` goes
  through the mod's `loadsave {id}` with the id resolved from DISK.
- Full write-up: `docs/05-driving-input.md`.

## hv assert — the TDD primitive

`hv assert --app raves --popup message --timeout 10` blocks until Raves reports a message
popup (exit 0) or dumps the actual state (exit 1). Conditions: `--node`, `--scene`, `--popup
[kind]`, `--present yes|no`, `--ocr-contains`. Use it to pin state before AND after a driven
action instead of screenshot-guess loops:
`hv goto qud in_game && hv assert --app qud --node in_game && <the actual test>`.

**`--node` tolerates landing DEEPER, and that tolerance is DIRECTIONAL.** Detection reports
the deepest match, so an edge aiming at a container legitimately lands on a child
(`title -> new_game` arrives on `game_mode`) — demanding the exact node would fail a drive
that worked. Going the other way the same tolerance is poison: `assert node=map_editor` is
satisfied by `me_menu_file`, the very state an escape edge exists to LEAVE, so that edge's
verify could not fail and `hv goto` reported success having moved nothing. Two ways to say
"and it actually moved": **`exact`** (the node, not a descendant) and **`not_within: X`**
(fail if we are at X or inside it). `_drive_route` attaches `not_within` by itself to any
edge whose target CONTAINS its origin, so climbing edges are honest without being authored
that way. Guarded by `tools/selftest_evaluate.py`.

## Gotchas

- `hv move` verifies by READBACK (CG window frame) — raw AX error codes lie for Godot's
  borderless window (kAXErrorFailure from sets that landed, and vice versa).
- **A click's `detail` echoes the button you ASKED for, not the events sent** — it is built from
  the request (`"%s click @ …" % button`), so `hv click --middle` prints `"middle click"` even
  when the running daemon is old enough to map it to left. It is not evidence the daemon has
  your backend change. Verify with a BEHAVIOUR only the new code produces (for `--middle`: does
  Raves' Map Editor context menu actually open?), never with the echoed field.
- **(Windows) `hv text` does not work on Godot** — it types into an editable element found through the
  accessibility layer, and Godot publishes none, so a Raves `LineEdit` fails with `no editable
  element found in target`. Type with **`hv key <target> <string>`** instead: an unnamed
  multi-char string falls through to `uiautomation.SendKeys`, which reaches the focused control
  whatever the toolkit. (Click the field first — SendKeys goes to focus, not to a target.)
- **(Windows) `hv key` modifiers are SendKeys syntax: `{Ctrl}a`, not `ctrl+a`.** The `+` form isn't
  rejected, it's TYPED — `hv key win ctrl+a` puts the six characters "ctrl+a" in the field, which
  looks like a silently ignored hotkey. Verified on a Raves `LineEdit`: "abcdef" → `{Ctrl}a` →
  `Z` leaves "Z". (Not WSH's `^a` either; uiautomation uses the braced form.) Single named keys
  are separate and case-insensitive — `hv key win escape` goes out as a real scan-code VK.
  (Both of these are facts about `uiautomation.SendKeys`, which is a Windows API the
  darwin backend does not use — they do not describe the Mac path.)
- **A SINGLE printable char is delivered differently depending on the destination, and it has to
  be.** To a top-level window (Godot/Unity, which expose no editable child) it goes as a VK
  keydown/keyup: the app translates the key itself and gets BOTH a `keycode` and the character.
  To a real EDIT child it goes as `WM_CHAR`, which is the only thing that inserts text there.
  Do **not** "fix" this by sending the full WM_KEYDOWN→WM_CHAR→WM_KEYUP sequence a real keystroke
  produces — measured on Raves, that types every character TWICE ("base" → "bbaassee"), because
  Godot already makes text out of the keydown. Until 2026-08-08 single chars were WM_CHAR-only
  everywhere, so `hv key win r` typed an "r" but could never fire an `event.keycode == KEY_R`
  binding — and still reported ok.
- A `[Errno 49] Can't assign requested address` from any hv call is TRANSIENT — just retry.
- Qud's window FREEZES when unfocused (Unity doesn't repaint) — `hv activate` + ~2s before a
  shot, or you'll diff a stale frame. The mod/bridge still runs unfocused; only pixels freeze.
- Shot scale = the display's backing (Retina 2× / 4K 1×) — see the ops quickref memory.
- Qud's MODERN menu screens (Records/Options/Mods) ignore ALL OS-synthesized keys —
  HID-sourced or not. Exit them with the mod's first-party `uiback` bridge command
  (gametree `{"bridge": "uiback"}` step / `{"dismiss": {..., "bridge": "uiback"}}`),
  never key injection. Clicks DO land (warp + HID button pair).
- **Qud's CHARGEN screens ignore the mod's own push APIs too, and that is the deeper
  version of the rule above.** `Keyboard.PushCommand`/`PushMouseEvent` feed the LEGACY
  console queue; the chargen module windows are modern `Qud.UI` windows that do not read
  it. Every tag form through either carrier moved exactly **0 pixels** — and zero every
  time with never a near-miss is the signature of a queue nobody reads, not of a wrong
  tag. Drive them with `{"bridge": "choose", "args": {"label": "Classic"}}`, which calls
  the window's own `GetSelections()` + selection handler in the mod (`UiDriver`). It
  matches by label, so it cannot land on the wrong card the way a coordinate can. The
  mod's `reflect` command dumps any live window's methods when a new screen needs one.
- Menu recipes click by LABEL, not coords: `{"click_text": "Records", "window": ...}` —
  fixed coords started stray games twice when the menu reflowed / the window sat
  off-slot. On Windows there is no OCR backend, so those edges carry the
  coordinate form beside the label one under `{"os": "nt"}` — add to the seam,
  do not overwrite the label. OCR matching is space-insensitive (Vision reads 'Opti ons' on Raves'
  Source Code Pro); optional `"offset": [dx,dy]` when the hit-area sits away from
  the caption (Qud Records' Back chevron is 40px above its "[Esc] Back" label).
- A dismiss step FAILS the recipe if the affordance is missing or the scene doesn't
  change — silent "dismissed" was how a stray Cancel reached the main menu (where
  Cancel == quit confirm). Fire ONE cancel and verify; never a fallback shotgun.
- **Modifier CLICKS and WHEELS need the modifier really HELD; a flag on the event is not
  enough.** Measured twice: Ctrl+wheel never opened Raves' panel, and Ctrl+click never fired
  the cell inspector while unmodified clicks worked perfectly. An old comment here claimed
  flags-only reached Godot — it does not, and believing it cost a FULL-run test case.
  `_hold_mods`/`_release_mods` press the real keys, released in a `finally`.
- A daemon re-exec mid key-combo orphans a modifier DOWN in the OS HID state: every later
  synthetic key/click arrives Cmd-modified and silently no-ops ("intermittent" app-side
  symptoms, survives app restarts). `_clear_stuck_mods` in darwin.py self-heals on every
  key/click op — if scripted keys ever go dead again, check `CGEventSourceFlagsState` first.
- Commit + push after each verified round; author guard before push:
  `git log --all --format='%ae' | grep -i allspice` must print nothing.
