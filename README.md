# highvisor — desktop automation for a single app, even unfocused

**highvisor lets scripts and AI agents inspect, capture, and control a specific macOS or Windows app —
even when that app is not in the foreground.** One local CLI/RPC gives you background window control,
screenshot capture, accessibility inspection (macOS AX / Windows UI Automation), synthetic input, and
cross-app visual-regression (screenshot diff / golden-image) testing.

Named after "hypervisor": it sits above the desktop and coordinates what runs on it.

## Quickstart (about a minute)

Requires **Python ≥ 3.9**. On **macOS**, grant the terminal running highvisor two permissions in
*System Settings → Privacy & Security*: **Accessibility** (for inspection/input) and **Screen Recording**
(for capture). When a call fails for lack of a grant, the failing operation tells you which permission is
likely missing (capture → Screen Recording; inspect/input → Accessibility).

```bash
pip install -e .        # installs Pillow + the per-OS backends (pyobjc on macOS, uiautomation on Windows)
hvd                     # start the daemon (control on 127.0.0.1:48720)  — or: python -m highvisor.server
hv ls                   # list windows:  win:<id>  pid=…  W×H  <title>
hv shot '<title>' shot.png     # capture one window (works while it's unfocused) → shot.png
hv activate '<title>'          # or drive it: click / text / key (see below)
```

`<target>` is a window ref — `win:38599`, `pid:1234`, or a title substring.

## What works now

Provenance: **✅ = verified in the named environment** (macOS, this machine, 2026-07-28) · **◐ =
implemented but not re-verified this pass** (app-dependent) · **— = unavailable**. The Windows code paths
are implemented; mark an individual Windows capability ✅ only with the tested Windows version + app + date.
This matters most for unfocused capture and synthetic input, where the app class changes the result.

| Capability | Command | macOS | Windows |
|---|---|---|---|
| List / find windows | `hv ls` | ✅ | ✅ (Win11 + Qud 1.0.5, 2026-08-06) |
| Capture an **unfocused** window | `hv shot` | ✅ (CoreGraphics `CGWindowListCreateImage`) | ✅ (`PrintWindow(PW_RENDERFULLCONTENT)` vs unfocused Unity/Qud, Win11, 2026-08-06; full screen `BitBlt`) |
| Accessibility inspect | `hv inspect` | ✅ AX | ◐ UI Automation |
| Move / resize / dock / stack | `hv move` `dock` `stack` | ✅ | ◐ |
| Synthetic input | `hv click` `text` `key` | ✅ (incl. Unity/Qud `--hover` click) | ✅ (Win11 + Qud 1.0.5, 2026-08-06 — `--hover` click with raw-input move; `key --focus` scan-code SendInput) |
| OCR a window | `hv ocr` | ✅ (Vision) | — (macOS only) |
| Visual parity / golden regression | `hv scene` `diff` `parity` | ✅ | ◐ |

**Background control is the hard part**, and it's tiered — highvisor reports **the delivery path it used**
(below). For semantic AX/UIA actions that's strong evidence; raw `key`/`click` posting is **best-effort**
until a screenshot or state readback confirms the app actually reacted (posting an event ≠ the app consumed
it):

| tier | mechanism | focus? |
|------|-----------|--------|
| 1 | accessibility action (UIA pattern / AX action) | background, semantic |
| 2 | window message post (`PostMessage` / `WM_SETTEXT`) | background, syntactic |
| 3 | cooperative hook (target polls our channel) | background, opt-in |
| 4 | activate + global input (`SendInput` / `CGEvent`) | steals focus; broadest but target-dependent |

## Choose your workflow

| I want to… | Start here |
|---|---|
| Control / capture **one app** | the Quickstart above → [`docs/05-driving-input.md`](docs/05-driving-input.md) |
| **Compare two apps** / catch UI regressions | [`docs/08-parity-kit.md`](docs/08-parity-kit.md) |
| **Coordinate AI agents** (Claude ↔ ChatGPT) | [`docs/06-agent-loop.md`](docs/06-agent-loop.md), [`docs/09-work-cycle.md`](docs/09-work-cycle.md) |
| Reach **another machine** | [`docs/07-ssh-transport.md`](docs/07-ssh-transport.md) (SSH), [`docs/04-web-and-bridge.md`](docs/04-web-and-bridge.md) (optional LAN bridge) |
| Understand / extend the design | [`docs/01-architecture.md`](docs/01-architecture.md) → [`docs/03-research-findings.md`](docs/03-research-findings.md) |

## How it works

Four layers: **brain adapters** → **CLI/clients** → **core daemon** (RPC server + single-threaded action
queue) → **per-OS `PlatformBackend`** (observe + act). Everything OS-specific lives behind the
`PlatformBackend` seam. The daemon speaks dependency-free framed JSON over localhost TCP
(`127.0.0.1:48720`) — any language can drive it in a few lines. See [`docs/01-architecture.md`](docs/01-architecture.md).

The CLI (`<target>` = window ref):

```bash
hv ping · ls · shot <t> [out.png] · click [--hover] <t> x y · text <t> <str> · key <t> <keys>
hv activate <t> · move <t> … · inspect <t> [depth] · ocr <t> · probe --app qud · raw '{"op":"ping"}'
```

## Visual parity & regression

Drive two apps to the same screen, localize where they differ, and catch layout regressions — the
toolchain built to bring a reconstruction 1:1 with its source (Raves of Qud vs Caves of Qud):

```bash
hv scene mods --parity --text   # drive BOTH apps + diff live vs the reference (match% + WHERE + side-by-side)
hv scene --all --bless          # lock goldens;  hv scene --all  re-checks for regressions
hv diff a.png b.png --regions   # ad-hoc screenshot diff: match% + ranked divergence + annotated image
```

Full workflows, the scenes-file format (incl. the `shell` setup step), and gotchas (the `--hover` click,
matched sizes, OCR limits) are in [`docs/08-parity-kit.md`](docs/08-parity-kit.md).

## Requirements

- **Python ≥ 3.9.**
- **macOS:** `pyobjc` frameworks (installed by `pip install -e .`); TCC grants for **Accessibility** and
  **Screen Recording** on the host process (the terminal, not the Python binary).
- **Windows:** `uiautomation` (installed by `pip install -e .`).

## Demo: the Notepad golem

`tools/gen_notepad_depth.py` turns a captured window into a runnable **golem** — a Godot reconstruction
that reflows like the source — from a UIA tree (`hv inspect`) + a capture (`hv shot`). Bundled fixtures
regenerate it with no capture step: `python tools/gen_notepad_depth.py` → `./notepad_golem`, then open in
Godot 4.7. Because the golem's popups are Controls drawn *inside* the window, `hv shot` captures them, so
you can verify hover/click states over the same RPC without stealing focus. Details in the script header.

## Docs & origin

[`docs/`](docs/) has the full design, numbered `00`–`09` (start at [`00-overview.md`](docs/00-overview.md)
for the task-oriented map). highvisor generalizes the per-project debug loop first built for
[raves-of-qud](https://github.com/TheMutantFactory/raves-of-qud) (a Godot viewer for Caves of Qud) into
one reusable service.
