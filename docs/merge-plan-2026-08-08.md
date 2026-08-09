# Merge plan — `dd/pc-lumpy-merge` → `main`, both repos (2026-08-08)

Written on the Mac against `highvisor@bbe412b` / `raves-of-qud@bfeb50e`, both `main`, both clean
and pushed. Companion note in the raves repo: `docs/merge-plan-2026-08-08.md` (pointer only).

**Everything below marked VERIFIED was executed on this machine today**, in throwaway clones under
the session scratchpad, with both working trees left untouched. Everything marked INFERRED is read
off a commit message or a code comment and has *not* been proven — those are called out by name.

---

## 0. The headline, and it is not what the brief assumed

**The branches merge textually CLEAN. Both of them. Zero conflicted files.** (VERIFIED: trial
`git merge --no-commit --no-ff` in scratch clones of both repos.)

The reason is that Lumpy has not been diverging — it has been *continuously merging `origin/main`*.
There are four such merges in highvisor and five in raves on that branch. The result:

| repo | main not in PC | PC not in main | merge-base |
|---|---|---|---|
| highvisor | **1** commit | 20 commits | `b707f2f` = `main~1` |
| raves-of-qud | **1** commit | 43 commits | `2484278` = `main~1` |

So the "20 vs 42 commits of parallel work landing in the same files" framing is wrong in the way
that matters. The Mac work the brief lists — directional assert tolerance, `_popup_matches` +
`refuse`, `_qud_command_chain`, refused-edge exclusion, the `report` signal, `quit_app`,
`plan.exclude`, the legacy-`goto` purge, the `{"game_live": false}` removal, `stranded_stage`,
`popup_n`/`ensure_popup`, the StartupHook `tab` sampler, the two-flag focus keeper,
`MapEditorDriver.OpenMenuName/CloseMenu` — **is already inside the PC branch and survives intact**.
(VERIFIED by grep and by structural diff; details in §2.)

Only ONE Mac commit per repo post-dates the merge-base:

- highvisor `bbe412b` — gametree: refresh the item-popup leaves after the box-model port
- raves `bfeb50e` — 1:1 popup: port Qud's popup BOX MODEL whole; placement 6.75 → 0.00

Neither is touched by the PC branch (`PopupOverlay.gd` is not in the merge's changed-file set).

**This makes the merge easy and the review hard, and the danger inverts.** A clean auto-merge means
git will *not* stop and ask about any of the eleven gametree edges the PC branch has rewritten. The
2026-08-07 lesson — each side's contribution had to be KEPT, not chosen between — applies here not
to conflict markers but to **changes that already picked a winner inside Lumpy's own merge
commits, and to eleven edges where the merge silently prefers the PC's implementation of something
the Mac has a green baseline for.** That is the whole content of §2 and §3.

### Merge-base moves — re-run the survey before executing

The brief says one more `PopupOverlay.gd` commit (titled-popup verification) is landing
concurrently, and `origin/dd/pc-lumpy-merge` in raves moved during the fetch for this survey
(`c6704b3..94a83ed`). **Step 0 of the plan re-runs the trial merge and re-diffs the eleven edges in
§2.2.** Do not trust these SHAs at execution time.

Author guard, run today: `git log --all --format='%ae' | grep -i allspice` prints nothing in both
repos, and every commit on `dd/pc-lumpy-merge` is `daniel.dee@gmail.com`. Clean. (VERIFIED.)

---

## 1. What the PC branch actually contains

### 1.1 highvisor — 20 commits, five themes

**A. Windows backend robustness — genuinely new, PC-only, no Mac counterpart.**
`list_targets` was rewritten to enumerate through Win32 `EnumWindows`/`GetWindowRect` instead of
UIA. The stated reason is that UIA property reads are cross-process COM calls into the target's UI
thread, so a Qud parked on a modal *blocks* rather than raising, and one hung app wedged the whole
daemon (three times in one session). `_restart_app` on `nt` now kills by PID off the app's own
windows rather than by image name, because a dev-run Raves *is* the Godot binary, so
`taskkill /IM RavesOfQud.exe` matched nothing and "restart" quietly became "launch another one".
Both are self-contained inside `backends/windows.py`. **Not testable here** (§5).

**B. Middle mouse button — new capability, HALF-LANDED.**
`hv click --middle` / `hv drag --middle` in the CLI, `_BUTTON_EVENTS` in the Windows backend.
**`backends/darwin.py` was not touched and has no middle-button path** — its click still does
`right = button == "right"`, so `--middle` on the Mac silently sends a LEFT click. (VERIFIED by
reading darwin.py.) See §3.1; this is the one place where "keep both" means "the Mac owes a
matching implementation", not "reconcile two of them".

**C. Liveness / staleness — new capability, cross-platform, the most valuable thing on the branch.**
A Unity or Godot window that has stopped rendering still screenshots; the compositor hands back the
last frame, which for Qud on a status screen is the bare playfield. The branch adds
`Engine._settle_rendering` (poll the app's `ui_age`, re-activating as needed), `hv shot --live`
(exit 1 on a stale capture, file still written), promotion of `ui_age` to the top level of a failed
`hv assert` with an explicit "restart the app before suspecting the recipe" message, and it
**replaces** the old `_qud_command(focus=True)` settle — "is Qud frontmost? if not, activate and
sleep a flat 2.0s" — with the same `ui_age` wait. The commit message claims a FULL 2 sweep failed
six of eight status tabs this way, each tab reporting the previous one. INFERRED (I have not
reproduced it), but the mechanism is sound and it matches a gotcha the Mac already recorded from
the other side (`docs/testing.md`: "a long-lived Qud can reach a state where uiQueue STOPS DRAINING
even while focused... the tell is ui_age climbing without bound").

**D. Stage restoration — new, cross-platform.** `layouts.remember_layout()` / `last_layout()`
persist the last applied layout name to `~/.config/highvisor/last_layout.json`; `hv layout` records
it and `_restart_app` re-applies it after readiness. Motivated by a Godot dev-run Raves relaunching
at the display default (4267×2400 on Lumpy) and producing a 2400-tall capture scored against a
1080-tall spec.

**E. `loadsave` answers "Mod Configuration Differs".** A save made without the bridge stops the load
on a popup whose *pre-selected* option is "Restart using save game's mod configuration" — i.e.
relaunch with our bridge disabled. The branch answers option index 1 once, inside `loadsave`'s own
wait, and on failure reports that a closed bridge means the mod looks disabled. **This is the one
piece of new engine logic that is stylistically at odds with the Mac's current code** — see §3.2.

**F. gametree.** Structurally the tree is nearly unchanged (VERIFIED, §2.2): 91 nodes both sides,
no nodes added or removed, **no `detect` block changed anywhere**, zero legacy `goto` recipes, one
new edge (`raves modding_toolkit → map_editor`). What *did* change is the `steps` of eleven raves
edges (click-by-label → click-by-coordinate; per-screen letter keys → F2 + coordinate tab click),
three `done` scores raised, and several very long `note` fields recording Lumpy's pixel-diff work.

**G. CLAUDE.md.** Three Windows-flavoured gotchas appended to Gotchas: a click's `detail` echoes the
button you *asked* for so it is not evidence the daemon has your backend change; `hv text` does not
work on Godot (no accessibility layer) — use `hv key`; `hv key` modifiers are SendKeys syntax
(`{Ctrl}a`), and the `ctrl+a` form is *typed*, not rejected. All three are Windows-path facts
appended to a shared file. Harmless, but they read as universal and are not — see §3.6.

### 1.2 raves-of-qud — 43 commits, six themes

**A. Map Editor per-object context menu and its dialogs — genuinely new capability.**
`MapEditorScreen.gd` +405 lines: `_open_context`/`_draw_context`/`_context_input` plus a full set of
Qud-modding-dialog reproductions (`_dlg_string`, `_dlg_pair`, `_dlg_info`, `_dlg_choice`, panel /
field / button styling). Hung off the **middle** button because Qud's own `DisplayContextInRegion`
has no caller and `OnClick` dispatches an unhandled `MiddleTile:x,y` — a deliberate divergence,
documented. Also adds right-click-as-eraser to match Qud's `RightTile:` branch. This is what theme
1.1.B exists to reach.

**B. ElliotSans extraction — genuinely new, cross-platform.** `tools/capture/fonts.py` (+196) carves
Qud's real UI TTFs out of `sharedassets0.assets` by parsing the sfnt table directory at each
candidate offset. Player-install → player-support-dir, never committed. This unblocked a
long-standing parity ceiling on every modern-UI screen. **This Mac already has the three ElliotSans
faces in `~/Library/Application Support/RavesOfQud/title/chrome/`** dated 2026-08-03 (VERIFIED), so
the Mac was already benefiting from an earlier hand-run of this; the branch makes it reproducible.

**C. Title-screen 1:1 corrections — `MainMenu.gd`, all gated on `Settings.one_to_one()`.** Link
column +2px (a 28px pad removed after the diff disproved it), hint line font 18→16 and six-space
separators → two, version corner loses the `raves x.y.z · hv …` line and drops 69px, backdrop
`_bg_nudge.scale` 1.0 → **1.010**. `_read_hv_version()` deleted as dead code.

**D. Status-tab parity work.** Seven new leaf specs
(`reports/2026-08-04-status-screens/parity-{attributes,journal,messagelog,quests,reputation,skills,tinkering}.json`),
a substantial reputation-pane rework (`StatusPaneFactions.gd` +172: paragraph breaks, tinted names,
content-sized rows, dashed column dividers), a tinkering keycap glyph + section rule, and — the one
that matters most to the Mac — **`StatusPaneInventory.gd`: centre the filter strip on the live
category count**. This is a PC *fix* for a defect the Mac had only *diagnosed*: `docs/testing.md`
records the `image` 38.49 mean as "the category filter strip, whose icons are offset by exactly one
slot... pre-existing and an ordering bug". See §3.3 — I believe this is a no-op on the Mac fixture,
and I can say why.

**E. Mod — `PlayerBridgeLoadAttach.cs`, genuinely new and important.** `PlayerBridgeMutator` is a
`[PlayerMutator]`, which runs on player *creation* and not on *load*, so any character made before
the bridge mod was installed came back with no `BridgePart` — and `BridgePart` is the only thing
that publishes. The failure mode is the nasty one: the server starts, commands work and log, and
nothing is ever published (snap.py blocks forever, Raves sits connected with empty panels). Plus
`StartupHook` heartbeat gains `clients` and `thread_focus` so a stall can be split three ways
instead of argued about.

**F. Harness — `UiState.gd` publishes `ui_age`** (frames *drawn*, via `Engine.get_frames_drawn()`,
explicitly not `_process` ticks, because Godot keeps ticking while occluded); `parity.py` gains a
first-class `capture` command that routes every shot through `hv shot --live` and pins the list
scroll by rebuilding the screen, a **size-mismatch** guard, and a **frozen-reference** guard;
`Main.gd` stops restoring a saved window size in 1:1 mode (the stage owns geometry); `goldens.json`
indexes a `pc-parity` golden save (index only, binary stays local); `docs/testing.md` grows ~1211
lines of PC FULL logs.

### 1.3 New capability vs PC port of Mac work — the split

Because Lumpy kept merging main, **there is almost no duplicated work**, and that is the good news.
The honest split:

| | items |
|---|---|
| **Genuinely new, no Mac counterpart** | Windows backend robustness · middle button (Windows half) · Map Editor context menu + dialogs · ElliotSans extraction · `PlayerBridgeLoadAttach` · `layouts` stage memory · `hv shot --live` + `_settle_rendering` · `parity.py capture` and its two guards · the seven status-tab specs |
| **Complementary — PC did the other half of something the Mac started** | Raves' `ui_age` (Qud's mod already published it on `main`; this mirrors it so one threshold works for both apps) · the inventory filter strip (Mac diagnosed, PC fixed) · heartbeat `clients`/`thread_focus` (Mac added the two-flag focus keeper, PC added the instrumentation to prove it is working) |
| **PC re-implementation of something the Mac already has WORKING** | **the eleven raves gametree edges** — this is the whole of §2.2 and the only place a resolution has to be argued |
| **Half-landed across the platform seam** | middle button (CLI + Windows, no darwin) · `qud_install_dir()` (added to `plat_win.py`, absent from `plat_mac.py`; `fonts.py` carries its own mac fallback so nothing breaks) |

---

## 2. Per-file conflict forecast

Textual conflicts: **none, in any file, in either repo** (VERIFIED). So for every file below the
question is only ever "are these two different implementations of the same intent, and does the
auto-merge silently pick one?"

### 2.1 `highvisor/engine.py` — additive; two shared-path behaviour changes

**Not a conflict.** The diff `main → merged` is +208/−28 and every Mac symbol survives: `not_within`
(9 hits), `_popup_matches`, `refuse`/`refused`, `_qud_command_chain`, the `report` signal,
`foreign`, `quit_app`. (VERIFIED by grep on the merged tree.) The PC added `_settle_rendering`, the
`--live` branch of `OP_SHOT`, the `ui_age` promotion in the assert timeout, the layout re-apply in
`_restart_app`, and the Windows-only `taskkill` change.

Two *replacements* on paths the Mac uses, and they need eyes rather than a resolution:

1. **`_qud_command(focus=True)`** no longer does "frontmost? else activate + sleep 2.0" — it calls
   `_settle_rendering(win, max_age=2, timeout=20)`. Better in principle. Note that
   **`_qud_command_chain` does not settle at all** — it opens its own socket and never activates
   (VERIFIED by reading it). That is not a regression (it never did), but the two Qud command paths
   now have different pre-conditions, and the quit chain is the one that runs against a game whose
   turn thread may be parked. Record it; do not "fix" it in the merge.
2. **`loadsave`'s popup answer** — see §3.2, the one place I would change code before landing.

`_settle_rendering` calls `self._read_state_file(p)` **without a pid**, so it reads the shared
`raves_state.json` rather than the per-process sidecar. With duplicate Raves instances that is the
coin flip the pid work exists to prevent. It degrades to a wrong `ui_age`, not a wrong screen, and
`hv goto` already refuses to drive with duplicates up — so: worth a follow-up, not a blocker.

**Keep-both, concretely:** take the file as merged. No hunk of Mac work is at risk here.

### 2.2 `highvisor/gametree.json` — THE file, and the only real argument

Structurally almost nothing moved (VERIFIED by a node-and-edge diff of `main` vs the merged tree):

- 91 nodes both sides; **no nodes added, none removed** (`stranded_stage` intact)
- **no `detect` block changed on any node** — the PC branch adds no detectors at all
- **zero `goto` recipes** — nothing for `selftest_plan.py` to reject
- one edge added: `raves modding_toolkit → map_editor`
- coordinate clicks / `click_text` steps: main `1 / 18` → merged `9 / 18→15`

That last row is the finding. **Eleven raves edges have had their `steps` rewritten, and the merge
takes the PC version of every one without asking.** They split into two groups that deserve
opposite treatment:

**Group 1 — `click_text` → fixed coordinates (3 edges). The PC's constraint is REAL.**

| edge | main | PC |
|---|---|---|
| `raves title → in_game` | `click_text "Continue"` | `click [958,578]` |
| `raves title → modding_toolkit` | `click_text "Modding Toolkit"` | `click_hover [150,902]` |
| `raves modding_toolkit → blueprint_browser` | `click_text "Blueprint Browser"` | `click_hover [866,537]` |

Commit `2eca769` says "click_text needs OCR the PC has not got". **VERIFIED: `backends/windows.py`
has no `ocr` method at all; only `darwin.py` defines one.** So on Lumpy these edges genuinely cannot
run as authored. But the Mac's CLAUDE.md rule is explicit and was paid for: *"Menu recipes click by
LABEL, not coords — fixed coords started stray games twice when the menu reflowed / the window sat
off-slot."* Taking the PC version wholesale re-imports the exact failure that rule exists to
prevent, on the machine where OCR works fine.

**Keep-both here means adding a per-OS seam to the step vocabulary, not choosing.** The engine
already conditions on `_os.name == "nt"` in three places, so the idiom exists; the *gametree* has
no such seam. Concrete proposal (small, and it makes the whole class of divergence expressible):

```json
{"click_text": "Continue", "window": "Raves of Qud", "os": "posix"},
{"click":      [958, 578], "window": "Raves of Qud", "os": "nt"}
```

with `_run_step` skipping any step whose `os` does not match `os.name`. Then both machines keep
their own working edge, `selftest_plan.py` still sees a route on both, and neither side's evidence
is discarded. **I have not written this**; it is a proposal, and Stage H is where it gets built and
covered by a selftest.

**Group 2 — per-screen letter key → F2 + coordinate tab click (7 edges: skills, attributes,
equipment, tinkering, journal, quests, reputation; plus a 1615→1619 nudge on messagelog).**
The PC's note on every one of these reads: *"Raves opens the status overlay with F2 and you pick a
TAB — it does not implement Qud's per-screen letter bindings (e/k/x/n/j/q), which is what this edge
used to send, so it reported every step OK and never arrived."*

**That claim is false about the current Raves source.** `godot/MainFrame.gd:282` (VERIFIED, on the
merged tree):

```gdscript
const STATUS_TAB_KEYS := {KEY_K: "skills", KEY_X: "attributes", KEY_TAB: "attributes",
	KEY_E: "equipment", KEY_I: "equipment", KEY_N: "tinkering", KEY_J: "journal",
	KEY_Q: "quests"}
```

All six letters are bound. And the Mac has a **fresh green baseline on exactly these edges**:
`docs/testing.md`, FULL 3 Raves Wander tour re-run 2026-08-08, **21/21 arrived, 0 EDGE, 0 ENV** —
driven with the letter-key edges.

The likely real cause on Lumpy is key *delivery*, not the binding: the same branch documents that
"a `KEYEVENTF_SCANCODE` modifier press is invisible to Godot (Godot tracks modifiers from key
MESSAGES and wants the VK form), so hv now emits BOTH forms" — a fix that landed *in this branch*,
plausibly after these edges were rewritten. **That is INFERRED from commit messages and the
MapEditor note; I have not confirmed the ordering against the PC's test logs.**

**Recommended resolution: keep `main`'s steps for the seven letter-key edges.** Do not take the
coordinate form on the Mac's evidence. Ask Lumpy to re-test the letter keys with the VK fix in
place; if they still fail there, express it through the same `os` seam as Group 1 rather than
overwriting. Two PC improvements inside these same steps ARE worth keeping regardless:
`{"key": "f2", "focus": true}` (the `focus` flag is new and correct) and the messagelog x-coordinate
re-measure — take those.

Everything else in the file — three `done` scores raised (`raves title` 0.90→0.96, `map_editor`
0.93→0.96, `blueprint_browser` 0.90→0.95), the long measurement notes, the new
`modding_toolkit → map_editor` edge — is documentation and additive routing. Take as merged. The
`done` numbers were measured on Lumpy against Lumpy's rendering; they are claims about the code, not
about the machine, so they carry over, but the Mac has no title-screen parity baseline to confirm
them (§3.5).

### 2.3 `highvisor/cli.py` — additive, one behaviour change

`--middle` on click/drag, `--live`/`--live-age`/`--live-timeout` on shot, and a `_button()` helper.
No Mac surface removed. **`_cmd_shot` now returns 1 on a stale capture** — only reachable when
`--live` is passed, so existing scripts are unaffected. Take as merged. `selftest_cli.py` passes on
the merged tree (VERIFIED).

### 2.4 `godot/UiState.gd` — complementary, no conflict

The Mac's `popup_n` serial and `ensure_popup()` and the PC's `ui_age` coexist in the merged file
(VERIFIED: `_popup_n`, `ensure_popup`, `_ui_age`, `_process` all present; the payload dict carries
`pid`, `ui_age`, `popup`, `popup_n`). The only cost is a new `_process` on an autoload. Take as
merged.

Operational consequence worth planning around: the **live `raves_state.json` on this machine has no
`ui_age`** (VERIFIED just now: keys are `mode, pid, popup, popup_n, scene, snap_ts, ts`), while
`qud_state.json` already has one. So `hv shot --live` will gate Qud immediately and will report
`--live skipped: state file has no ui_age` for Raves **until Raves is rebuilt and relaunched**.
It degrades cleanly (no failure), but a `parity.py capture` run before the Raves rebuild is only
half-gated. Stage E orders the rebuild first for this reason.

### 2.5 `mod/StartupHook.cs` — additive, no conflict

The PC adds `clients` and `thread_focus` to the heartbeat plus two `*Safe()` readers. The Mac's
Map-Editor `tab` sampler is untouched and present (`string tab = ""` / `PopupBridge.StatusTab` /
`_uiMenu`), and `ui_age` was already there on `main`. The two-flag focus keeper is in `Bridge.cs`,
which the PC branch does not touch — both `XRLCore.bThreadFocus` and `GameManager.focused` are
still held (VERIFIED at `Bridge.cs:536,540-542`). `mod/MapEditorDriver.cs` is not in the merge's
changed-file set at all, so `OpenMenuName()`/`CloseMenu()` are safe. Take as merged.

**VERIFIED: `dotnet build mod/RavesOfQudBridge.csproj` on the merged tree — 0 errors, 18 warnings,
all pre-existing `CS0618` obsolescence notices.** That is the SPOT mod-API-drift check passing
against *this Mac's* Qud assembly, which is the thing that could most plausibly have differed:
`PlayerBridgeLoadAttach.cs` uses `[HasCallAfterGameLoaded]`/`[CallAfterGameLoaded]` and
`Bridge.Server.ClientCount`, all written against Windows Qud 1.0.5.

### 2.6 `godot/MainMenu.gd` — no conflict, but unverifiable here

Title-screen 1:1 geometry, all `Settings.one_to_one()`-gated, plus `_read_hv_version()` deleted.
Nothing the Mac has touched since the merge-base. Take as merged — with the caveat in §3.5 that
these numbers were fitted on Lumpy and the Mac has no title baseline to check them against.

### 2.7 `godot/Main.gd` — no conflict

Two guards: don't *write* the stage's window size back to settings in 1:1 mode, and don't *restore*
a saved size in 1:1 mode. Both correct and consistent with the Mac's "the stage owns geometry"
policy. Take as merged.

### 2.8 `tools/capture/parity.py` — no conflict, and the baselines survive it

This is the file that could have invalidated the whole "known good before" asset, so it was checked
directly rather than reasoned about:

- **VERIFIED: `main`'s `parity.py` and the merged `parity.py` produce byte-identical `--json` score
  output** on the committed Equipment baseline captures.
- **VERIFIED: the merged tool reproduces both committed scoreboards EXACTLY** —
  Equipment 33/33 leaves at `max|delta| = 0.0000`, item popup 7/7 at `0.0000`.
- **VERIFIED: the new FROZEN REFERENCE guard does not trip on either baseline** — the qud/qud2 pairs
  differ on 380,291 px and 1,093,010 px respectively, nowhere near identical.

So the scorer is unchanged and the Mac's two pinned baselines remain valid instruments after the
merge. Take as merged.

### 2.9 Files with no Mac counterpart at all

`backends/windows.py`, `layouts.py`, `mod/PlayerBridgeLoadAttach.cs`, `tools/capture/fonts.py`,
`tools/capture/plat_win.py`, `godot/MapEditorScreen.gd`, `godot/StatusPaneFactions.gd`,
`godot/StatusPaneTinkering.gd`, `godot/StatusPaneInventory.gd`, `godot/BlueprintBrowserScreen.gd`,
the seven new specs, `fixtures/goldens.json`, `docs/testing.md`, both `CLAUDE.md` files. All
additive. `docs/testing.md` auto-merged (the Mac's 2026-08-08 sections and the PC's log interleave;
worth one read-through for ordering, not for content).

**VERIFIED on the merged tree: all eight changed `.gd` files pass `Godot --check-only`
individually, and `Main.gd`'s deep check is clean** (no "Could not parse global class" anywhere).

---

## 3. Specific risks

Taking the brief's checklist first, since three of the four resolve cleanly and it is worth saying
so explicitly.

> *Does the PC branch add or rely on gametree detectors that the `{"game_live": false}` removal has invalidated?*

**No. VERIFIED — not one `detect` block differs between `main` and the merged tree.** The PC branch
adds no detectors at all. The five surviving `"game_live": false` occurrences are identical on both
sides and are all *conjuncts alongside an `ocr_any` match* (chargen, load game, ended runs, the
toolkit list, the quit confirm) — they narrow a positive OCR hit rather than resting on an inference
alone, which is exactly the distinction `main`'s note draws. The standalone fallback on `title` is
gone on both sides. Nothing to do.

> *Does it carry legacy `goto` recipes that `tools/selftest_plan.py` will now reject?*

**No. VERIFIED — zero `goto` recipes in the merged tree, and `selftest_plan.py` PASSES on it.**
All four highvisor selftests pass on the merged tree (`plan`, `evaluate`, `state_read`, `cli`), and
all four also pass on `main`, so that is a like-for-like green.

> *Does its `UiState.gd` conflict with `popup_n`?*

**No.** §2.4 — complementary fields, both present in the merged file.

> *Does its `StartupHook.cs` touch the focus keeper or the heartbeat's state-file fields?*

**Heartbeat fields yes (two added, none changed); focus keeper no** — the keeper lives in
`Bridge.cs`, untouched. §2.5.

> *Does its Windows backend assume an engine API the Mac side has changed?*

**No.** `backends/windows.py` implements the `PlatformBackend` surface and the merge does not change
that surface. The Windows-specific engine branches (`_os.name == "nt"`) are self-contained.

### The risks I *did* find, in priority order

**3.1 `hv click --middle` is a lie on macOS.** (VERIFIED.) The CLI advertises it, the Windows
backend implements it, `darwin.py` does not — `button` is reduced to `right = button == "right"`,
so middle silently becomes left. And the PC's own new CLAUDE.md gotcha says the response `detail`
field will still print `"middle click"`, so **the failure is invisible from the CLI output**. The
raves feature that needs it (the Map Editor per-object context menu, +405 lines) is therefore
**unreachable and unverifiable on this machine** until `darwin.py` gains a middle-button path
(`kCGEventOtherMouseDown/Up` with `kCGMouseButtonCenter`). Stage H owns this. Until then, do not
record "Map Editor context menu: not working on Mac" — it has not been tested, because it cannot be.

**3.2 `loadsave`'s new popup answer is a fallback shotgun, on a path where the Mac has a content
matcher.** The PC code fires `{"name":"popup","action":"option","index":1}` whenever
`"popup" in str(signals["scene"]).lower()` during the load wait. Two problems:

- The trigger is a **substring test on the scene name**, and Qud's heartbeat sets
  `scene` to the raw view name — `StartupHook.cs:209` computes `popup` as
  `view.IndexOf("Popup", ...) >= 0`, so *any* Qud popup view satisfies it, not the Mod
  Configuration one.
- **Index 1 is hardcoded.** On a different popup, index 1 is a different button. Pressing the wrong
  option on a Qud modal during a save load is not a cosmetic failure.

The Mac spent this week building the machinery for exactly this: `_popup_matches` matches by KIND,
`_qud_command_chain` matches by **CONTENT** ("Matching is by CONTENT, never by the popup's id"), and
the `refuse` condition exists so a harness can decline a modal it does not recognise. And
`CLAUDE.md` already says: *"A dismiss step FAILS the recipe if the affordance is missing... Fire ONE
cancel and verify; never a fallback shotgun."*

**Recommendation: keep the PC's capability and re-express its trigger.** Gate on the popup's
message text containing "mod configuration" (the branch's own comment names the string, so the data
is there), and select the option by its **label** — "Load keeping current mod configuration" — not
by index. Concretely, route it through the same read loop `_qud_command_chain` already uses. **I
have not written this patch**, and this is the one item in the plan I would not merge unmodified.
It is low-frequency (only fires inside `loadsave`, only when a popup is up) so it is not a
merge-blocker — but it should not reach `main` in its current form.

**3.3 The inventory filter-strip recentre — almost certainly a no-op on the Mac fixture, and here
is why that is checkable.** This is the PC change most likely to move a Mac parity number, because
`filter_image`/`filter_frame`/`filter_cell` are three of the Equipment spec's leaves. Reading the
constants (VERIFIED):

```
FILT_MAX_CELLS = 12   # the ALL cell + 11 categories
FILT_SPAN_FULL = 739.0 ; FILT_CENTRE = 959.5 ; FILT_ALL_DX = 28.0 ; FILT_BADGE_EDX = 720.0
_filt_left(cells) = 959.5 - (739 - (12 - cells)*58)/2
```

At `cells == FILT_MAX_CELLS`: `_filt_left(12) = 959.5 - 369.5 = 590.0`, so the ALL cell draws at
`590 + 28 = 618 == FILT_X`, the Q badge at `590 == FILT_BADGE_QX`, the E badge at
`590 + 720 == 1310 == FILT_BADGE_EX`. **Identical to the old hardcoded constants.** The PC's own
comment says the Mac fixture has 11 categories, which gives `cells = min(11+1, 12) = 12`.

So: **if `sync-raves-and-qud` really presents 11 categories, the Equipment scoreboard's filter
leaves must reproduce the 2026-08-08 baseline within noise. If they move, the fixture is not
11-category and this change has real geometry consequences on the Mac.** That is a sharp,
falsifiable prediction and it is the single highest-value thing in the FULL 2 gate (Stage F).

**3.4 The eleven rewritten edges.** §2.2. The risk is not that they are wrong on Lumpy — it is that
the auto-merge installs them on the Mac, where seven of them replace edges with a 21/21 green
baseline from yesterday, and three of them replace label-clicking with the coordinate-clicking the
Mac's CLAUDE.md forbids by name.

**3.5 Title-screen geometry is fitted on Lumpy and has no Mac baseline.** `MainMenu.gd`'s
`_bg_nudge.scale = 1.010`, the +2 / +6 / +69 offsets, and the hint font 18→16 were all measured on
Lumpy at 1920×1080 against Lumpy's Qud. The Mac has **no title parity spec and no title
scoreboard** — `reports/` covers Equipment and the item popup only. So `raves title` going 0.90 →
0.96 in the gametree is a claim the Mac cannot check.

Also: **this Mac has a machine-local `~/Library/Application Support/RavesOfQud/title_bg.json`
containing `{"dx":0,"dy":0,"sx":1,"sy":1}`** (VERIFIED). `MainMenu` polls that file live, so the
local override will *fight* the new baked-in 1.010 scale — the title backdrop on this machine will
keep rendering at the old fit until that file is deleted or updated. The PC's blueprint note even
says its results were obtained "with the machine-local override deleted". **Deleting it is a
prerequisite for any title measurement on the Mac** and belongs in Stage E.

**3.6 The shared `CLAUDE.md` now carries Windows-path facts stated universally.** "`hv text` does
not work on Godot" and "`hv key` modifiers are SendKeys syntax" are true of the *Windows* backend —
`uiautomation.SendKeys` is a Windows API and the Mac backend does not use it. Left as written they
will mislead a future Mac session. Small edit, but do it as part of the merge rather than after:
prefix both with **(Windows)**.

**3.7 Two smaller things, recorded so they are not rediscovered.**
- `_settle_rendering` and `loadsave` both call `_read_state_file(path)` with **no pid**, bypassing
  the per-process sidecar the Mac added. Degrades to a possibly-wrong `ui_age` under duplicate
  instances, not to a wrong screen.
- `plat_win.py` gains `qud_install_dir()`; `plat_mac.py` has no such function. `fonts.py` carries
  its own macOS fallback (`getattr(plat, "qud_install_dir", None)`), so **nothing breaks**
  (VERIFIED), but the seam is asymmetric. Add `qud_install_dir()` to `plat_mac.py` and let
  `fonts.py` drop its fallback — cleanup, not a blocker.
- `apps.qud.state_file` / `apps.raves.state_file` in `gametree.json` are **macOS paths**
  (`~/Library/Application Support/...`). The PC branch did not change them, and `_settle_rendering`
  hard-depends on that key resolving. Either Lumpy patches the tree locally or `--live` silently
  no-ops there. **Unresolved — ask Lumpy**; I could not determine which from the diff.

---

## 4. The staged plan

The asset this plan is built around is that **the Mac has a known-good "before", measured today**:

| baseline | where | what it pins |
|---|---|---|
| **B1** Equipment parity | `raves reports/2026-08-08-parity-baseline/` (captures + `scoreboard.json` + README) | 33 leaves, reproducible to ±0.01 (mean −0.001) |
| **B2** Item-popup parity | `raves reports/2026-08-05-item-popup/` | 7 leaves, reproduced EXACTLY (+0.00) |
| **B3** SPOT 5/5 | `raves docs/testing.md` | typing guard · state-graph render · Godot parse+`_ready` · Main.gd deep check · `dotnet build` |
| **B4** highvisor selftests 4/4 | `highvisor tools/selftest_*.py` | plan · evaluate · state_read · cli |
| **B5** whole-tree tours | `docs/testing.md` FULL 3, 2026-08-08 | Wander: raves **21/21**; Classic w/ refused-edge exclusion: qud **28/28**, raves **20/21** (the one non-arrival, raves `continue`, is a documented artefact of reloading before every node) |
| **B6** FULL 4 mod round-trip | `docs/testing.md` 2026-08-07 evening | popups mirror+answer, `statustab`, nav commands |

Every gate below names which of these it is compared against. **A stage that cannot be compared to a
baseline is not a gate** — say so and move on rather than inventing a number.

### Stage 0 — re-survey (5 min, no apps)

Both remotes moved during this survey and `main` is expected to gain a `PopupOverlay.gd` commit.

1. `git fetch --all --prune` in both repos; re-check `git rev-list --left-right --count main...origin/dd/pc-lumpy-merge`.
2. Re-run the trial merge in a **scratch clone** (`git clone --no-hardlinks` into the scratchpad;
   do not add a worktree to the real repo).
3. Re-run the edge diff from §2.2. If the count of changed edges is no longer 11, re-read before
   proceeding.
4. Author guard: `git log --all --format='%ae' | grep -i allspice` — must print nothing.

**Gate:** the merge is still conflict-free and the changed-edge set is still understood. If a
textual conflict has appeared, this document's §2 is stale — re-read the conflicting file before
resolving anything.

### Stage A — cut the branches

```
highvisor:     git switch -c dd/mac-pc-merge-0808 main
raves-of-qud:  git switch -c dd/mac-pc-merge-0808 main
```

Fresh branches off `main`, not a reuse of `dd/mac-pc-merge` (which holds the 2026-08-07 merge and
would muddy the comparison). Nothing is pushed until Stage G.

### Stage B — merge highvisor, then fix the three things that should not land as-is

1. `git merge --no-ff origin/dd/pc-lumpy-merge`
2. **Revert the seven letter-key edges** in `gametree.json` to `main`'s steps (§2.2 Group 2),
   *keeping* the PC's `{"key":"f2","focus":true}` improvement and the messagelog `1619`
   re-measurement. Record the reason in the edge `note` so the next session does not undo it.
3. **Re-express the `loadsave` popup answer** by content + label rather than scene-substring +
   index 1 (§3.2).
4. **Prefix the two Windows-only CLAUDE.md gotchas with "(Windows)"** (§3.6).
5. Leave the three `click_text → coordinate` edges (Group 1) **as the PC wrote them for now** — they
   are wrong for the Mac but Stage H is where the seam that fixes both lands, and shipping them
   temporarily is visible and reversible. Alternative if Stage H slips: revert those three to
   `click_text` too, and let Lumpy carry a local patch until the seam exists. Decide at Stage B, do
   not leave it implicit.

**Gate B — compare against B4.** All four selftests must pass:
`selftest_plan` · `selftest_evaluate` · `selftest_state_read` · `selftest_cli`.
*Already proven to pass on the unmodified merge (VERIFIED today), so a failure here is caused by
the edits in steps 2–4, which narrows it usefully.* Seconds, no apps, no daemon.

### Stage C — merge raves

1. `git merge --no-ff origin/dd/pc-lumpy-merge`
2. Read `docs/testing.md` once end to end for section ordering — the Mac's 2026-08-08 entries and
   the PC's log interleave and the auto-merge has no opinion about chronology.

**Gate C — compare against B3, the three SPOT checks that need no running app:**

| check | expected, and why |
|---|---|
| `python3 tools/regression/typing_guard_audit.py` | **PASS, with the field inventory going 14 → 15** — the new entry is `MapEditorScreen.gd LineEdit` and nothing else. (VERIFIED today on the unmodified merge.) A *different* delta means a text field arrived that this plan did not predict. |
| `Godot --headless --path godot/ --check-only --script res://Main.gd` | clean; `Identifier not found: Settings`/`QudLauncher` are the documented false positives. (VERIFIED.) |
| `dotnet build mod/RavesOfQudBridge.csproj` | **0 errors**, 18 pre-existing `CS0618` warnings. (VERIFIED — this is the check that clears `PlayerBridgeLoadAttach.cs` against *this Mac's* Qud assembly.) |

The remaining two SPOT checks — the state-graph render test and `--quit-after 120` — **were not run
during this survey** because both instantiate autoloads, `UiState._ready` writes into the shared
support dir, and both apps were live at the time. Run them at the top of Stage D, with the apps
down.

### Stage D — SPOT, complete, apps down

`hv quit raves && hv quit qud` (or leave them down from the start), then:

- `Godot --headless --path godot/ --script res://tests/state_graph_render.gd`
- `Godot --headless --path godot/ --quit-after 120`

**Gate D — compare against B3: SPOT must be 5/5.** Anything less stops the merge here. Afterwards,
delete the stray `raves_state.*.json` these two runs will have written, so the first `hv state` of
Stage E is not reading a headless corpse.

### Stage E — deploy and relaunch (no measurement yet)

Order matters, and each step exists because measuring before it makes the measurement meaningless.

1. **Deploy the mod and FULLY restart Qud** — mods compile at startup, so nothing measured before
   this means anything (the 2026-08-07 merged-FULL entry makes this point explicitly). Confirm the
   bridge is on 48710 and `Player.log` has no `MODERROR`.
2. **Rebuild Raves** (`tools/build_macos.sh`). Required, not optional: `UiState.gd`'s `ui_age` is
   the gate for `hv shot --live`, the currently-running Raves does not publish it (VERIFIED — the
   live `raves_state.json` has no `ui_age` key), and the exported app freezes scripts at build time.
3. **Delete `~/Library/Application Support/RavesOfQud/title_bg.json`** (§3.5) — the machine-local
   override will otherwise mask `MainMenu`'s new 1.010 backdrop scale.
4. `hv install-daemon` / let the source watcher pick up the engine changes; confirm with a behaviour
   only the new code has (`hv shot CavesOfQud /tmp/x.png --live` should print `[live ui_age=…]`),
   **not** by uptime or pid — `os.execv` preserves both.
5. `hv launch raves`, `hv layout pair`, `hv state`.

**Gate E:** `hv state` names both apps' screens with `report=fresh`; `hv shot --live` reports a real
`ui_age` for Qud **and now for Raves too**. No baseline comparison — this stage only establishes
that the harness is talking to the new builds.

### Stage F — FULL 2, the parity gate. This is the stage the baselines exist for.

Run the Equipment spec first, on the pinned fixture, with the pin re-driven exactly as
`reports/2026-08-08-parity-baseline/README.md` records it: `sync-raves-and-qud` (Wander, Joppa
`JoppaWorld.11.22.1.1.10`) via `tools/capture/fixture.py reload`, Equipment tab both apps, filter
ALL, `--stable` second Qud capture.

Use the PC's new `parity.py capture status_equipment <prefix>` for this — it is the reason that
command exists, and it pins the list scroll by rebuilding the screen.

**Gate F1 — compare against B1 (Equipment, 33 leaves).** The prediction, stated in advance so the
result can falsify it:

- **`filter_image` / `filter_frame` / `filter_cell` must reproduce within the spec's ~0.7 noise.**
  §3.3 shows the recentre is arithmetically a no-op at 12 cells. **If these leaves move, stop** —
  the fixture is not 11-category and the PC's change has real Mac geometry consequences that need
  measuring, not absorbing.
- **The fixture-independent control set — `doll_frame[0..4]`, `filter_frame[1..4]`, `outer_frame` —
  must stay within ±1 of B1.** Use *only* these as controls. `list_cat`/`list_item` are **not**
  chrome; `docs/testing.md` records getting that wrong once already.
- Nothing on this branch touches a rendering path used by the equipment doll, so `doll_image[0..4]`
  should be flat.

**Gate F2 — compare against B2 (item popup, 7 leaves).** The PC branch does not touch
`PopupOverlay.gd`, so this is a pure regression check on the Mac's own box-model port landing
alongside PC changes. Re-drive the pin (`fixture.py twiddle robe`, cloth robe, by NAME) and expect
the same +0.00 reproducibility B2 recorded. **Note the ordering hazard:** B2's scoreboard was taken
*after* the box-model port, and `main`'s concurrent titled-popup commit may move these leaves
legitimately. If it does, that is `main`'s delta, not the merge's — establish it on `main` **before**
Stage B so the two causes stay separable.

**Not gated, because there is nothing to compare to:** the seven new status-tab specs ship with no
Mac captures and no scoreboards (`reports/2026-08-04-status-screens/` has `*_qud.png` from an
earlier Mac session and the specs, but no `scoreboard.json` for any of them). Scoring them now
produces numbers with no "before". Establishing Mac baselines for those seven is **follow-on work,
not a merge gate** — Stage I.

### Stage G — FULL 3 and FULL 4, then push

**Gate G1 — compare against B5.** Re-run the Wander whole-tree tour for **raves** first: it is the
one that covers the seven reverted status edges, and B5 says 21/21 with 0 EDGE / 0 ENV. Anything
below 21/21 points straight at Stage B step 2. Then the Qud Wander tour. Keep the tour's
environment sampling (bridge reachable, `qud_state.json` inside its 6s TTL) so an ENV failure is not
misread as an EDGE failure — that distinction is what made the 08-07 phantom failures legible.

The Classic tours (qud 28/28, raves 20/21) are ~36 min of wall time, 56–70% of it save reloads.
**Run them only if a Wander tour regresses, or before an actual release** — B5 is explicit that this
tour shape is pre-release, not per-merge.

**Gate G2 — compare against B6 (FULL 4).** Popups mirror and answer (`CmdSystemMenu` → Qud popup →
Raves `popup=menu` → answered), `statustab` on two tabs, the three nav commands each verified by
effect. Also exercise **`hv loadsave`** deliberately here, since Stage B step 3 rewrote it.

**Gate G3 — FULL 1, and it grew by one field.** The typing-guard inventory went 14 → 15; the new
field is the Map Editor's blueprint filter `LineEdit`. Type `e j q x n 1 2` into it and **read the
characters back out of the field** — never infer from the scene not moving, which is the documented
trap and which has now bitten twice. The Map Editor's new modal dialogs (`_dlg_string`, `_dlg_pair`)
also carry fields; cover at least one.

Then: commit, `git log --all --format='%ae' | grep -i allspice` (must print nothing), push both
branches, PR into `main`.

### Stage H — the two seams the merge exposes (separate branch, after the merge lands)

Neither belongs inside the merge; both are the merge's real output.

1. **A per-OS seam in the gametree step vocabulary** (§2.2). Add an `os` field to steps, skip
   non-matching steps in `_run_step`, restore `click_text` for the Mac on the three Group-1 edges
   with the PC coordinates alongside, and extend `selftest_plan.py` to prove every target is
   reachable **under each `os` value independently** — otherwise the seam can silently strand one
   platform.
2. **Middle-button support in `backends/darwin.py`** (§3.1) —
   `kCGEventOtherMouseDown`/`Up` with `kCGMouseButtonCenter`. Until this exists the Map Editor
   context menu is untestable on the Mac, and `hv click --middle` reports success while doing
   something else, which is the worst available failure mode.

Smaller, same branch: `qud_install_dir()` in `plat_mac.py`; pid-aware reads in `_settle_rendering`
and `loadsave`; ask Lumpy how `apps.*.state_file` resolves on Windows.

### Stage I — follow-on, not gates

Mac baselines for the seven new status-tab specs (captures + `--stable` + a recorded pin + a
scoreboard + a declared per-leaf `fixture_dependent` control set, following the discipline the item
popup baseline established). A title-screen spec and baseline, so `raves title` 0.96 becomes
checkable on the Mac.

---

## 5. What cannot be verified on this machine

**Cannot be tested here at all — must be tested on Lumpy:**

- The Windows backend entirely: `EnumWindows`-based `list_targets` (and the claim that it fixes the
  daemon wedging on a hung app), the PID-based `taskkill`, `_BUTTON_EVENTS`, the SendKeys behaviours
  the new CLAUDE.md gotchas describe. None of this code path executes on macOS.
- The three Group-1 `click_text → coordinate` edges: their *justification* is Windows' lack of OCR,
  which cannot be reproduced on a Mac that has Vision.
- Whether the seven letter-key status edges now work on Lumpy with the VK/scancode modifier fix in
  place. **This is the question that decides §2.2 Group 2, and only Lumpy can answer it.**
- `apps.*.state_file` resolution on Windows, and therefore whether `hv shot --live` and
  `_settle_rendering` do anything at all there (§3.7).
- The `pc-parity` golden save (`Lumpy-true-kin-dev-char`) — `goldens.json` indexes it, the binary is
  local to Lumpy. Any PC FULL 2 number in `docs/testing.md` measured on it is unreproducible here by
  construction, and should not be compared against B1.
- The "Mod Configuration Differs" popup itself: it arises from a save whose mod configuration
  differs, which is a Lumpy-side condition. **§3.2's rewrite can be reasoned about here but only
  end-to-end tested there** — so land it, and ask Lumpy to exercise it.

**The Mac can prove alone, and several of these are already proven (today, on the unmodified
merge):**

- Textual mergeability of both repos — **VERIFIED, zero conflicts.**
- Every Mac symbol survives the merge — **VERIFIED** (§2.1, §2.4, §2.5).
- No detector changed, no `goto` recipe returned, one edge added — **VERIFIED** (§2.2).
- highvisor selftests 4/4 — **VERIFIED**, and identical to `main`'s result.
- The typing-guard audit and its 14 → 15 field inventory — **VERIFIED.**
- `Godot --check-only` on `Main.gd` and on all eight changed `.gd` files — **VERIFIED clean.**
- `dotnet build` against this Mac's real Qud assembly, 0 errors — **VERIFIED**, which clears
  `PlayerBridgeLoadAttach.cs` and the two new `StartupHook` readers for API drift.
- The scorer is unchanged and both pinned baselines reproduce at `max|delta| = 0.0000` —
  **VERIFIED**, so B1 and B2 remain valid instruments after the merge.
- Everything in Stages D through G: SPOT 5/5, FULL 1–4 against B1/B2/B3/B5/B6 on this machine's
  fixtures.

**Verifiable here in principle, but not yet possible:** the Map Editor per-object context menu — it
is hung off the middle button, and the Mac backend cannot produce one (§3.1). It becomes Mac-testable
after Stage H item 2, and not before. Do not record it as failing in the meantime; it is untested.
