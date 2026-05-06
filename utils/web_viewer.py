"""Tiny HTTP server + viewer page that renders the execution graph live.

Why this exists
---------------
PAL runs over MCP stdio, so a long panel call is opaque to the operator —
all you see is "task started" and a wall of JSON when it finishes. The
execution graph captures everything as it happens; this module just exposes
it over HTTP so you can watch a debate unfold in a browser.

Design choices
--------------
- **stdlib only.** ThreadingHTTPServer in a daemon thread. No new deps,
  zero risk to the MCP stdio loop, the server dies cleanly with the
  process.
- **Polling, not SSE.** The HTML page polls /runs every 2s and the
  selected /runs/<id>/tree every 1.5s while it's "running". Simpler than
  SSE-via-stdlib, robust to disconnects, and 1.5s feels live enough for
  multi-minute panel runs.
- **Single self-contained HTML file.** Embedded as a string below — no
  asset directory, no template engine, one round-trip to load.
- **Optional, never load-bearing.** PAL_WEB_DISABLE=1 to skip startup;
  any boot failure logs and is swallowed (graph viewer is observability,
  not a hard dep — same posture as the graph itself).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


_DEFAULT_PORT = int(os.environ.get("PAL_WEB_PORT", "8765"))
_BIND_HOST = os.environ.get("PAL_WEB_HOST", "127.0.0.1")  # local-only by default
_DISABLED = bool(os.environ.get("PAL_WEB_DISABLE"))
# Default ON so the operator gets zero-effort visibility on every PAL launch.
# Set PAL_WEB_AUTO_OPEN=0 (or any falsy value) if you don't want the browser
# tab popped each time Claude Code restarts the MCP server.
_AUTO_OPEN = os.environ.get("PAL_WEB_AUTO_OPEN", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)

# Module-level state for the running server, so callers can probe its URL.
_SERVER: Optional[ThreadingHTTPServer] = None
_SERVER_THREAD: Optional[threading.Thread] = None
_SERVER_PORT: Optional[int] = None
_BOOT_LOCK = threading.Lock()


def get_server_url() -> Optional[str]:
    """Return the live viewer URL, or None if the server isn't running."""
    if _SERVER_PORT is None:
        return None
    return f"http://{_BIND_HOST}:{_SERVER_PORT}/"


# ---------------------------------------------------------------------------
# Static viewer page
# ---------------------------------------------------------------------------

_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>PAL Execution Graph</title>
<style>
  :root {
    --bg: #0e0f12; --fg: #e7e9ee; --muted: #8b8fa1; --accent: #7aa2f7;
    --good: #9ece6a; --bad: #f7768e; --warn: #e0af68; --card: #1a1b21;
    --border: #2a2c33;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--fg); margin: 0;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
         font-size: 13px; }
  header { padding: 12px 16px; background: #16171c; border-bottom: 1px solid var(--border);
           display: flex; align-items: center; gap: 16px; }
  header h1 { margin: 0; font-size: 14px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; }
  main { display: grid; grid-template-columns: 360px 1fr; height: calc(100vh - 50px); }
  #runs { overflow-y: auto; border-right: 1px solid var(--border); }
  .run-row { padding: 10px 14px; border-bottom: 1px solid var(--border); cursor: pointer; }
  .run-row:hover { background: #1f2027; }
  .run-row.selected { background: #1f2730; border-left: 3px solid var(--accent); padding-left: 11px; }
  .run-row .row-top { display: flex; justify-content: space-between; gap: 8px; }
  .run-row .tool { font-weight: 600; }
  .run-row .label { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .badge { padding: 1px 6px; border-radius: 3px; font-size: 10px; text-transform: uppercase;
           font-weight: 600; letter-spacing: 0.4px; }
  .b-running { background: #2a3a5e; color: var(--accent); }
  .b-completed { background: #25382a; color: var(--good); }
  .b-failed { background: #3d1f24; color: var(--bad); }
  .b-cancelled { background: #3d2f1f; color: var(--warn); }
  .cost { color: var(--muted); font-size: 10px; margin-top: 2px; }
  .cost-oauth_free { color: var(--good); }
  .cost-oauth_fallback_paid { color: var(--warn); }
  .cost-api_paid { color: #c0caf5; }
  #detail { overflow-y: auto; padding: 18px 22px; }
  #detail h2 { margin: 0 0 8px; font-size: 16px; }
  #detail .header-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  #detail .meta-row { color: var(--muted); font-size: 12px; margin: 4px 0 12px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 6px;
          padding: 12px 14px; margin: 12px 0; }
  .card .card-h { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .card .card-h .name { font-weight: 600; }
  .card.depth-1 { margin-left: 18px; border-left: 2px solid #2c3242; }
  .card.depth-2 { margin-left: 36px; border-left: 2px solid #3a3142; }
  .card.depth-3 { margin-left: 54px; border-left: 2px solid #423142; }
  .edge-tag { color: var(--muted); font-size: 10px; text-transform: uppercase;
              padding: 1px 6px; border-radius: 3px; background: #20232b; }
  .events { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
            color: var(--muted); margin-top: 8px; }
  .events div { padding: 2px 0; }
  pre { background: #14151a; padding: 10px; border-radius: 4px; overflow-x: auto;
        max-height: 300px; font-size: 11px; }
  .empty { color: var(--muted); padding: 30px; text-align: center; }
  .rollup { display: flex; gap: 12px; margin: 8px 0; }
  .rollup span { padding: 3px 10px; background: #1f2027; border-radius: 4px; font-size: 11px; }
  .controls { display: flex; gap: 8px; align-items: center; }
  .controls input { background: #14151a; border: 1px solid var(--border); color: var(--fg);
                    padding: 4px 8px; border-radius: 4px; font-size: 12px; }
  .controls select { background: #14151a; border: 1px solid var(--border); color: var(--fg);
                     padding: 4px 8px; border-radius: 4px; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>PAL Execution Graph</h1>
  <span class="meta" id="dot">●</span>
  <span class="meta" id="conn">connecting…</span>
  <div class="controls" style="margin-left:auto;">
    <select id="filter-status">
      <option value="">all statuses</option>
      <option value="running">running</option>
      <option value="completed">completed</option>
      <option value="failed">failed</option>
      <option value="cancelled">cancelled</option>
    </select>
    <input id="filter-tool" placeholder="filter tool name" />
  </div>
</header>
<main>
  <aside id="runs"><div class="empty">loading…</div></aside>
  <section id="detail"><div class="empty">Select a run on the left.</div></section>
</main>
<script>
let SELECTED = null;
let LAST_RUNS_HASH = "";
const $ = (q) => document.querySelector(q);

async function fetchRuns() {
  const status = $('#filter-status').value;
  const tool = $('#filter-tool').value.trim();
  const params = new URLSearchParams();
  params.set('limit', '100');
  if (status) params.set('status', status);
  if (tool) params.set('tool_name', tool);
  const r = await fetch('/runs?' + params.toString());
  if (!r.ok) throw new Error('runs ' + r.status);
  const body = await r.json();
  return body.runs || [];
}

function statusBadge(status) {
  return `<span class="badge b-${status}">${status}</span>`;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
}

function fmtElapsed(start, end) {
  if (!start) return '';
  const ref = end || (Date.now() / 1000);
  const sec = ref - start;
  if (sec < 1) return Math.round(sec * 1000) + 'ms';
  if (sec < 60) return sec.toFixed(1) + 's';
  return Math.floor(sec / 60) + 'm ' + Math.round(sec % 60) + 's';
}

function renderRunRow(r) {
  const cost = r.cost_tier ? `<div class="cost cost-${r.cost_tier}">${r.cost_tier}</div>` : '';
  const sel = (SELECTED === r.run_id) ? ' selected' : '';
  return `<div class="run-row${sel}" data-run="${r.run_id}">
    <div class="row-top">
      <span class="tool">${r.tool_name}</span>
      ${statusBadge(r.status)}
    </div>
    <div class="label">${r.label || r.run_id.slice(0,12) + '…'} · ${fmtElapsed(r.started_at, r.completed_at)}</div>
    ${cost}
  </div>`;
}

async function renderRunsList() {
  try {
    const runs = await fetchRuns();
    const hash = JSON.stringify(runs.map(r => [r.run_id, r.status, r.completed_at]));
    if (hash === LAST_RUNS_HASH) return;
    LAST_RUNS_HASH = hash;
    const html = runs.length
      ? runs.map(renderRunRow).join('')
      : '<div class="empty">no runs yet</div>';
    $('#runs').innerHTML = html;
    document.querySelectorAll('.run-row').forEach(el => {
      el.addEventListener('click', () => selectRun(el.dataset.run));
    });
    $('#dot').style.color = '#9ece6a';
    $('#conn').textContent = `${runs.length} run${runs.length === 1 ? '' : 's'} · ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    $('#dot').style.color = '#f7768e';
    $('#conn').textContent = 'connection lost — retrying';
  }
}

function renderRollup(rollup) {
  const items = Object.entries(rollup).map(([k, v]) =>
    `<span class="cost-${k}">${v} ${k}</span>`).join('');
  return items ? `<div class="rollup">${items}</div>` : '';
}

function renderEvents(events) {
  if (!events || !events.length) return '';
  const lines = events.map(e => {
    const t = new Date(e.ts * 1000).toLocaleTimeString();
    const m = (e.message || '').replace(/</g, '&lt;');
    return `<div>[${t}] ${e.event_type}: ${m}</div>`;
  }).join('');
  return `<div class="events">${lines}</div>`;
}

function renderTreeNode(node, depth = 0) {
  const cost = node.cost_tier ? `<span class="cost cost-${node.cost_tier}">${node.cost_tier}</span>` : '';
  const edge = node.edge_kind ? `<span class="edge-tag">${node.edge_kind}</span>` : '';
  const elapsed = fmtElapsed(node.started_at, node.completed_at);
  const args = node.args_json ? `<details><summary>args</summary><pre>${escapeHtml(node.args_json)}</pre></details>` : '';
  const result = node.result_json ? `<details><summary>result</summary><pre>${escapeHtml(node.result_json)}</pre></details>` : '';
  const error = node.error ? `<details open><summary style="color:#f7768e">error</summary><pre>${escapeHtml(node.error)}</pre></details>` : '';
  const events = renderEvents(node.events);
  const childrenHtml = (node.children || []).map(c => renderTreeNode(c, depth + 1)).join('');
  const cls = depth > 3 ? 'depth-3' : `depth-${depth}`;
  return `<div class="card ${cls}">
    <div class="card-h">
      <span class="name">${node.tool_name}</span>
      ${edge}
      ${statusBadge(node.status)}
      ${cost}
      <span class="meta" style="color:var(--muted);font-size:11px;">${node.label || ''} · ${elapsed}</span>
    </div>
    ${args}${result}${error}${events}
    ${childrenHtml}
  </div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}

async function renderDetail() {
  if (!SELECTED) {
    $('#detail').innerHTML = '<div class="empty">Select a run on the left.</div>';
    return;
  }
  try {
    const r = await fetch('/runs/' + SELECTED + '/tree');
    if (!r.ok) {
      $('#detail').innerHTML = '<div class="empty">run not found</div>';
      return;
    }
    const body = await r.json();
    const tree = body.tree;
    const rollup = body.cost_tier_rollup || {};
    $('#detail').innerHTML = `
      <div class="header-row">
        <h2>${tree.tool_name}</h2>
        ${statusBadge(tree.status)}
        <span class="meta" style="color:var(--muted);">${tree.label || tree.run_id.slice(0,16)+'…'}</span>
      </div>
      <div class="meta-row">
        started ${fmtTime(tree.started_at)} · ${fmtElapsed(tree.started_at, tree.completed_at)}
      </div>
      ${renderRollup(rollup)}
      ${renderTreeNode(tree)}
    `;
  } catch (e) {
    $('#detail').innerHTML = '<div class="empty">error: ' + e.message + '</div>';
  }
}

function selectRun(rid) {
  SELECTED = rid;
  document.querySelectorAll('.run-row').forEach(el => {
    el.classList.toggle('selected', el.dataset.run === rid);
  });
  renderDetail();
}

$('#filter-status').addEventListener('change', () => { LAST_RUNS_HASH = ''; renderRunsList(); });
$('#filter-tool').addEventListener('input', () => { LAST_RUNS_HASH = ''; renderRunsList(); });

renderRunsList();
setInterval(renderRunsList, 2000);
setInterval(() => { if (SELECTED) renderDetail(); }, 1500);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Routes a tiny set of paths to the execution graph + the static page.

    Everything is read-only and unauthenticated. Bound to 127.0.0.1 by
    default so it's not exposed beyond the operator's machine. If anyone
    flips PAL_WEB_HOST to 0.0.0.0 they're opting into that themselves.
    """

    # Quiet the default per-request stderr log line; we have our own logger.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("web: " + format, *args)

    # ---- helpers ------------------------------------------------------

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        self._send_json({"status": "error", "error": "not found"}, status=404)

    def _graph(self):
        from utils.execution_graph import get_graph
        return get_graph()

    # ---- routing ------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(_INDEX_HTML)
            return

        if path == "/health":
            self._send_json({"status": "ok"})
            return

        graph = self._graph()
        if graph is None:
            self._send_json(
                {"status": "error", "error": "execution graph is disabled"},
                status=503,
            )
            return

        if path == "/runs":
            from urllib.parse import parse_qs

            qs = parse_qs(parsed.query)
            limit = int(qs.get("limit", ["50"])[0])
            status = (qs.get("status", [None])[0]) or None
            tool_name = (qs.get("tool_name", [None])[0]) or None
            try:
                rows = graph.list_runs(limit=limit, status=status, tool_name=tool_name)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"status": "error", "error": str(exc)}, status=500)
                return
            self._send_json({"status": "ok", "count": len(rows), "runs": rows})
            return

        # /runs/<id>            → run + events
        # /runs/<id>/tree       → recursive tree + cost rollup
        if path.startswith("/runs/"):
            parts = path[len("/runs/"):].split("/", 1)
            run_id = parts[0]
            sub = parts[1] if len(parts) > 1 else ""
            if not run_id:
                self._not_found()
                return
            run = graph.get_run(run_id)
            if run is None:
                self._not_found()
                return
            if sub == "tree":
                from tools.graph_query import _cost_tier_rollup

                tree = graph.get_run_tree(run_id)
                rollup = _cost_tier_rollup(tree) if tree else {}
                self._send_json({"status": "ok", "tree": tree, "cost_tier_rollup": rollup})
                return
            if sub == "":
                run["events"] = graph.get_run_events(run_id)
                self._send_json({"status": "ok", "run": run})
                return
            self._not_found()
            return

        self._not_found()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _pick_port(start: int) -> int:
    """If start is taken, walk forward up to +20 ports. Reasonable for a
    laptop; if all of those are busy something else is wrong."""
    for port in range(start, start + 20):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((_BIND_HOST, port))
            sock.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"web viewer: no free port in {start}..{start + 20}")


def start_web_viewer() -> Optional[str]:
    """Boot the viewer in a daemon thread. Returns the URL, or None if
    disabled / failed. Idempotent — calling twice returns the existing URL."""
    global _SERVER, _SERVER_THREAD, _SERVER_PORT

    if _DISABLED:
        logger.info("web viewer disabled (PAL_WEB_DISABLE)")
        return None

    with _BOOT_LOCK:
        if _SERVER is not None:
            return get_server_url()
        try:
            port = _pick_port(_DEFAULT_PORT)
            server = ThreadingHTTPServer((_BIND_HOST, port), _Handler)
        except Exception as exc:  # noqa: BLE001
            logger.warning("web viewer boot failed (%s); UI disabled for this process", exc)
            return None
        thread = threading.Thread(
            target=server.serve_forever,
            name="pal-web-viewer",
            daemon=True,
        )
        thread.start()
        _SERVER = server
        _SERVER_THREAD = thread
        _SERVER_PORT = port
        url = get_server_url()
        logger.info("web viewer started at %s", url)
        if _AUTO_OPEN and url:
            _try_open_browser(url)
        return url


def _try_open_browser(url: str) -> None:
    """Open the URL in the operator's default browser. Best-effort —
    headless / SSH / container environments return False without raising.

    Runs in a separate thread because some platforms block briefly while
    spawning the browser process; we don't want PAL startup to wait."""
    import webbrowser

    def _go():
        try:
            opened = webbrowser.open(url, new=2, autoraise=False)
            logger.info("auto-opened browser tab: opened=%s url=%s", opened, url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-open failed (%s); user can navigate manually", exc)

    threading.Thread(target=_go, name="pal-web-autoopen", daemon=True).start()


def stop_web_viewer() -> None:
    """Shut the viewer down at process exit. atexit-safe."""
    global _SERVER, _SERVER_THREAD, _SERVER_PORT
    with _BOOT_LOCK:
        if _SERVER is None:
            return
        try:
            _SERVER.shutdown()
            _SERVER.server_close()
        except Exception:  # noqa: BLE001
            pass
        _SERVER = None
        _SERVER_THREAD = None
        _SERVER_PORT = None
