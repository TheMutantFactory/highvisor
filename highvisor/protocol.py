"""Wire protocol for the highvisor daemon.

Same framing as the raves-of-qud bridge (deliberately — it is simple, language
agnostic, and already battle-tested): each message is

    [4-byte big-endian length][UTF-8 JSON body]

Requests are ``{"op": "<name>", ...}``; responses are ``{"ok": bool, ...}``.
Screenshots ride back as base64 in ``png_b64`` (JSON has no bytes). Keep this
module dependency-free so any client language can reimplement it in a few lines.
"""
import json
import struct

HOST = "127.0.0.1"          # localhost only, single machine — never bind public
PORT = 48720                # arbitrary high port (raves used 48710; avoid clash)
BRIDGE_PORT = 48722         # LAN-facing peer bridge: token-gated, DATA ONLY (never
                            # exposes control ops). Discovered over mDNS/zeroconf.

# Op names the engine understands. Clients send op=<one of these>.
OP_PING = "ping"            # -> {ok, backend, version}
OP_LIST = "list_targets"    # -> {ok, targets:[Target]}
OP_SHOT = "screenshot"      # target -> {ok, png_b64, bytes, w, h}
OP_ACTIVATE = "activate"    # target -> ActionResult
OP_TEXT = "text"            # target, text -> ActionResult
OP_KEY = "key"              # target, keys -> ActionResult
OP_CLICK = "click"          # target, x, y (window-relative), [button, double, hover, modifiers] -> ActionResult
OP_DRAG = "drag"            # target, x1, y1, x2, y2, [button, steps, modifiers] -> ActionResult
OP_SCROLL = "scroll"        # target, x, y (window-relative), [dy, dx, modifiers] -> wheel event
OP_MOUSE = "mouse"          # target, x, y (window-relative) -> warp+move ONLY, no click — hover-state capture
OP_INSPECT = "inspect"      # target, depth -> {ok, tree}
OP_OCR = "ocr"              # target -> {ok, w, h, text, boxes:[{text,bbox}]} (Vision)
OP_MOVE = "move"            # target, (zone | x,y,w,h), [topmost] -> ActionResult
OP_STACK = "stack"          # top, bottom, [gap] -> stack `top` directly above `bottom`
OP_DOCK = "dock"            # target -> apply the standing dock rule for this window
OP_PROBE = "probe"          # app (profile) | window,[port] -> {running, state, window, port_open}
OP_SCREEN = "screen_size"   # -> {ok, w, h}  (physical pixels of primary display)
OP_DISPLAYS = "displays"    # -> {ok, displays:[{id, x, y, w, h, main}]}  (all displays, points)
OP_LAYOUT_LIST = "layout_list"    # -> {ok, layouts:[{name, description, placements}]}
OP_LAYOUT_APPLY = "layout_apply"  # name -> {ok, applied, results:[...]}
OP_LAYOUT_SAVE = "layout_save"    # name, [description] -> {ok, saved, path, windows}
OP_PEERS = "peers"                # -> {ok, peers:[{name,host,port}], self} (bridge)
OP_PEER_SHOT = "peer_shot"        # peer, target -> {ok, png_b64, bytes} (via bridge)
OP_LAUNCH = "launch"              # name (launcher) | raw spec -> ActionResult
OP_LAUNCH_LIST = "launch_list"    # -> {ok, launchers:{name: spec}}
OP_LAUNCH_SAVE = "launch_save"    # name, spec -> {ok, saved, path}
OP_GAMETREE = "gametree"          # -> {ok, tree} (the canonical game state-machine tree)
OP_GAMESTATE = "gamestate"        # [ocr] -> {ok, states:{app:{node,label,path,off,running,via}}}
OP_GAMEGO = "gamego"              # app, node -> drive the app to that tree state (planned route)
OP_PLAN = "plan_route"            # app, node, [from] -> the route gamego WOULD take, without driving
OP_TRACE = "trace"                # limit -> the last N goto runs: what each steered by, did, reached
OP_ASSERT = "assert_state"        # app, [node|scene|popup|ocr_contains|present], timeout -> {ok, pass, actual, elapsed}
OP_WRITE_TEXT = "write_text"      # path (under $HOME), content -> {ok, path, bytes} (small config writes)
OP_QUDWISH = "qudwish"            # wish -> {ok, wish} — run a Caves of Qud wish via the Raves mod bridge
OP_QUDBRIDGE = "qudbridge"        # name, args -> {ok} — ANY first-party mod command, un-wrapped
OP_QUDBACK = "qudback"            # -> {ok} — first-party Escape/close for Qud's modern menus (bridge uiback)
OP_QUD_SAVES = "qud_saves"        # -> {ok, saves:[{row,name,guid,location,mode,saved}]} (from DISK, no game needed)
OP_LOAD_SAVE = "load_save"        # name -> restart-to-title if needed, click Continue + the row BY NAME -> {ok,row}
OP_QUIT = "quit_app"              # app [force] -> stop EVERY instance and leave it stopped
OP_RESTART = "restart_app"        # app (qud|raves) -> kill ALL instances, launch solo, wait for the window
OP_RUN_TEST = "run_test"          # [node], test -> run a REGISTERED check -> {exit, out, seconds}
OP_GRANT_INPUT = "grant_input"    # -> raise the Accessibility prompt for the DAEMON process
OP_ABORT = "abort_control"        # -> release focus/mouse NOW + refuse control ops for 30s (the panic path)

MAX_FRAME = 64 * 1024 * 1024  # 64 MiB guard (a 4k screenshot fits easily)


def send_frame(sock, obj):
    """Serialize obj to JSON and write one length-prefixed frame."""
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_frame(sock):
    """Read one framed message; return the parsed dict, or None on clean EOF."""
    hdr = _recvn(sock, 4)
    if hdr is None:
        return None
    (n,) = struct.unpack(">I", hdr)
    if n > MAX_FRAME:
        raise ValueError("frame too large: %d bytes" % n)
    body = _recvn(sock, n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _recvn(sock, n):
    """Read exactly n bytes; None if the peer closed before n arrived."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
