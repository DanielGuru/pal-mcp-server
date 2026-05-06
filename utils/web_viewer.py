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

# Opt-in gate for non-localhost binds. The viewer has no auth — anyone
# on the network with access to the bound interface can read every
# prompt, response, diff, and file snippet from the execution graph.
# That's safe by default (127.0.0.1 → only the local user) but the
# operator MUST consciously opt in before exposing it. Anything that
# isn't localhost requires PAL_WEB_ALLOW_REMOTE=1, otherwise we refuse
# to start. This blocks accidental exposure (e.g. someone setting
# PAL_WEB_HOST=0.0.0.0 because they're tunnelling and forgetting that
# anyone on the LAN can hit it). Full token auth is on the open queue
# for when someone actually needs remote.
_LOCALHOST_BINDS = {"127.0.0.1", "::1", "localhost"}
_ALLOW_REMOTE = (os.environ.get("PAL_WEB_ALLOW_REMOTE", "") or "").strip().lower() in (
    "1", "true", "yes", "on",
)
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

_INDEX_HTML = r"""<!doctype html>
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
  /* Conversation-first layout. The viewer's job is to show the panel
     debate as a readable transcript — the rest is collapsible noise. */
  header { padding: 10px 16px; background: #16171c; border-bottom: 1px solid var(--border);
           display: flex; align-items: center; gap: 14px; }
  header h1 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.3px; color: var(--muted); }
  header .dot { font-size: 14px; color: var(--good); }
  header .dot.live { color: var(--accent); animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  header .conn { color: var(--muted); font-size: 11px; }
  header .port { color: var(--muted); font-size: 11px; padding: 2px 8px;
                 background: #1f2027; border-radius: 3px; font-family: ui-monospace, monospace; }
  header .port.orphan { color: var(--bad); background: #2a1a1f; }
  header .controls { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  header select { background: #14151a; border: 1px solid var(--border); color: var(--fg);
                  padding: 5px 8px; border-radius: 4px; font-size: 12px; min-width: 280px; }
  header button { background: transparent; border: 1px solid var(--border); color: var(--muted);
                  padding: 5px 10px; border-radius: 4px; font-size: 11px; cursor: pointer; }
  header button:hover { color: var(--fg); border-color: #444; }
  header button.active { color: var(--accent); border-color: var(--accent); }
  main { max-width: 920px; margin: 0 auto; padding: 22px 24px 80px; }
  .summary { color: var(--muted); font-size: 12px; margin-bottom: 18px;
             padding-bottom: 14px; border-bottom: 1px solid var(--border); }
  .summary .verdict-tally { display: inline-flex; gap: 8px; margin-left: 10px; }
  .summary .verdict-tally span { padding: 2px 8px; background: #1f2027; border-radius: 3px;
                                 font-size: 10px; text-transform: uppercase; letter-spacing: 0.3px; }
  /* Transcript blocks (panelist answers + judge synthesis) */
  .transcript { margin: 14px 0; padding: 12px 16px;
                background: var(--card); border-left: 3px solid var(--speaker);
                border-radius: 4px; }
  .transcript .transcript-h { font-family: -apple-system, sans-serif;
                              font-size: 12px; font-weight: 600;
                              color: var(--speaker); margin-bottom: 6px;
                              display: flex; align-items: baseline; gap: 10px; }
  .transcript .transcript-h .who { font-weight: 700; letter-spacing: 0.2px; }
  .transcript .transcript-h .when { color: var(--muted); font-size: 10px; font-weight: 400; }
  .transcript .transcript-body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                                 font-size: 13px; line-height: 1.55;
                                 color: var(--fg); white-space: pre-wrap;
                                 word-wrap: break-word; }
  /* Per-panelist colour palette — assigned by name hash so codex/gemini/etc.
     each get a stable colour throughout the debate. */
  .speaker-codex { --speaker: #79b8ff; }
  .speaker-gemini { --speaker: #b392f0; }
  .speaker-grok { --speaker: #f08c5d; }
  .speaker-host { --speaker: #34c5b7; }
  .speaker-judge { --speaker: #f9c149; }
  .speaker-other { --speaker: #8b8fa1; }
  .transcript-judge { background: #1c1a14; border-left-width: 4px; }
  /* In-progress streaming — italic + dimmed body so the operator can
     visually tell they're watching live writing, not the final answer. */
  .transcript-streaming .transcript-body { color: #aab1c5; font-style: italic;
                                           opacity: 0.85; }
  /* Single soft "thinking" line for live runs that haven't produced a
     transcript event yet. Replaces the wall of file_read / tool_use pings. */
  .thinking { display: flex; align-items: center; gap: 10px;
              color: var(--muted); font-style: italic; font-size: 12px;
              padding: 14px 16px; margin: 14px 0; }
  .thinking::before { content: '○'; color: var(--accent); animation: pulse 1.4s ease-in-out infinite; }
  /* Raw mode: show the old tree view for debugging. Off by default. */
  .raw .transcript, .raw .thinking { display: none; }
  .raw .raw-tree { display: block; }
  .raw-tree { display: none; }
  .raw-tree pre { font-size: 10px; line-height: 1.4; max-height: none;
                  background: #0c0d10; padding: 12px; border-radius: 3px;
                  white-space: pre-wrap; word-wrap: break-word; }
  /* Verdict tally chips */
  .v-land { color: var(--good); }
  .v-needs-changes { color: var(--warn); }
  .v-reject { color: var(--bad); }
  .badge { padding: 1px 6px; border-radius: 3px; font-size: 10px; text-transform: uppercase;
           font-weight: 600; letter-spacing: 0.4px; }
  .b-running { background: #2a3a5e; color: var(--accent); }
  .b-completed { background: #25382a; color: var(--good); }
  .b-failed { background: #3d1f24; color: var(--bad); }
  .b-cancelled { background: #3d2f1f; color: var(--warn); }
  .empty { color: var(--muted); padding: 60px 20px; text-align: center; font-size: 13px; }
  .empty .hint { color: #555; font-size: 11px; margin-top: 8px; }
</style>
</head>
<body>
<header>
  <h1>PAL · panel transcript</h1>
  <span class="dot" id="dot">●</span>
  <span class="conn" id="conn">connecting…</span>
  <span class="port" id="port" title="Viewer port. Each PAL process owns its own; orphan instances may listen on prior ports.">port —</span>
  <div class="controls">
    <select id="run-picker"><option>loading runs…</option></select>
    <button id="raw-toggle" title="Toggle raw tree view">raw</button>
  </div>
</header>
<main id="content">
  <div class="empty">waiting for a panel run…
    <div class="hint">fire <code>multiaudit</code> or <code>panel</code> from PAL and the conversation will appear here</div>
  </div>
</main>
<script>
// =============================================================================
// PAL panel transcript viewer
// Walks the run tree, flattens transcript events from every panelist, sorts
// by timestamp, renders as colour-coded blockquotes. Status pings are hidden
// behind the "raw" toggle. Auto-scrolls the latest message into view.
// =============================================================================

let SELECTED = null;
// MANUAL_PICK locks SELECTED once the user explicitly chooses a run from the
// picker (or a deep-link via ?run=<id> sets one). While false, every refresh
// of the picker re-evaluates and switches to the newest running root — so a
// freshly-fired multiaudit overtakes a stale run from the previous PAL session
// without the user having to click. Without this, SELECTED was set the FIRST
// time the picker rendered (often before the new run was registered) and then
// stuck on the older row forever.
let MANUAL_PICK = false;
let LAST_RUNS_HASH = "";
let RAW_MODE = false;
const $ = (q) => document.querySelector(q);

// Deep-link: /?run=<id> pins a specific run (used by multiaudit / web_url to
// hand back a URL that lands on the run that was just dispatched, instead of
// whatever the viewer happens to auto-pick).
(function applyDeepLink() {
  try {
    const params = new URLSearchParams(window.location.search);
    const id = params.get('run');
    if (id) { SELECTED = id; MANUAL_PICK = true; }
  } catch (_) {}
})();

async function fetchRuns() {
  const r = await fetch('/runs?limit=100');
  if (!r.ok) throw new Error('runs ' + r.status);
  const body = await r.json();
  return body.runs || [];
}

// Show the port we're actually serving on. Each PAL process picks its own
// (PAL_WEB_PORT walks +20 if taken), so users with multiple PAL sessions
// may have several viewers running on different ports. When THIS viewer's
// graph shows no recent activity, flag the port red so the user notices
// they're probably on a stale tab from an older PAL session.
function updatePortIndicator(runs) {
  const portEl = $('#port');
  if (!portEl) return;
  const port = window.location.port || '?';
  const recent = runs.find(r => {
    const ts = r.completed_at || r.started_at || 0;
    return ts && (Date.now() / 1000 - ts) < 600; // run in the last 10min
  });
  if (!recent) {
    portEl.classList.add('orphan');
    portEl.textContent = `port ${port} · stale?`;
    portEl.title = `No runs in the last 10 minutes on this viewer. Check the PAL boot log for the active port — orphan viewers from previous sessions may still be listening on this one.`;
  } else {
    portEl.classList.remove('orphan');
    portEl.textContent = `port ${port}`;
    portEl.title = `Viewer port ${port} (PAL_WEB_PORT walks +20 if taken).`;
  }
}

function statusBadge(status) {
  // Class names limited to a safe alphabet so a malicious status can't
  // escape the class attribute or inject a new one. Display text escaped
  // separately. statusBadge runs BEFORE escapeHtml/safeClass are defined
  // textually in the script — both are forward-references resolved at
  // call time, which works because JS hoists function declarations.
  const cls = safeClass(status);
  return `<span class="badge b-${cls}">${escapeHtml(status || '')}</span>`;
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

// -- run picker -----------------------------------------------------------

// Observation tools — these are introspection calls, not actual work, and
// should never appear in the picker. The picker is for runs with substance:
// panels, audits, debates, code reviews. A user polling task_status 30 times
// should not produce 30 picker entries that drown out the actual debate.
const OBSERVATION_TOOLS = new Set([
  'task_status',
  'task_result',
  'cancel_task',
  'list_runs',
  'get_run',
  'run_tree',
  'web_url',
  'version',
  'listmodels',
  'apilookup',
]);

function isInterestingRoot(r) {
  // A "root" with no parent that's worth showing in the picker. Filters
  // out fast observation tools and 0ms no-op completions that drown out
  // real panel/audit runs.
  if (r.parent_run_id) return false;
  if (OBSERVATION_TOOLS.has(r.tool_name)) return false;
  return true;
}

async function renderRunPicker() {
  try {
    const runs = await fetchRuns();
    const roots = runs.filter(isInterestingRoot);
    const hash = JSON.stringify(roots.map(r => [r.run_id, r.status, r.completed_at]));
    if (hash !== LAST_RUNS_HASH) {
      LAST_RUNS_HASH = hash;
      const picker = $('#run-picker');
      if (!roots.length) {
        picker.innerHTML = '<option>no panel runs yet — fire multiaudit or panel</option>';
      } else {
        picker.innerHTML = roots.map(r => {
          const live = r.status === 'running' ? ' · live' : '';
          const elapsed = fmtElapsed(r.started_at, r.completed_at);
          const label = r.label || r.tool_name + ':' + (r.run_id || '').slice(0, 6);
          return `<option value="${escapeAttr(r.run_id)}">${escapeHtml(label)} · ${escapeHtml(elapsed)}${live}</option>`;
        }).join('');
      }
      // Auto-select the newest running root (or newest root) and keep
      // following the head of the list until the user picks something
      // manually. MANUAL_PICK locks the choice so we never yank the user
      // away from what they were reading mid-debate.
      if (!MANUAL_PICK && roots.length) {
        const live = roots.find(r => r.status === 'running');
        const target = (live || roots[0]).run_id;
        if (target !== SELECTED) {
          SELECTED = target;
          picker.value = SELECTED;
          renderConversation();
        } else {
          picker.value = SELECTED;
        }
      } else if (SELECTED) {
        // Keep the picker's selected option in sync. If the user's
        // SELECTED run scrolled off the head of the list (limit=100),
        // gracefully stop forcing it.
        if (roots.find(r => r.run_id === SELECTED)) {
          picker.value = SELECTED;
        }
      }
    }
    const liveCount = runs.filter(r => r.status === 'running').length;
    $('#dot').classList.toggle('live', liveCount > 0);
    $('#conn').textContent = liveCount
      ? `${liveCount} live · ${new Date().toLocaleTimeString()}`
      : `${roots.length} run${roots.length === 1 ? '' : 's'} · ${new Date().toLocaleTimeString()}`;
    updatePortIndicator(runs);
  } catch (e) {
    $('#dot').classList.remove('live');
    $('#dot').style.color = '#f7768e';
    $('#conn').textContent = 'connection lost — retrying';
  }
}

// -- transcript -----------------------------------------------------------
function speakerClass(label) {
  const lower = String(label || '').toLowerCase();
  if (lower.startsWith('judge')) return 'speaker-judge';
  if (lower.includes('codex')) return 'speaker-codex';
  if (lower.includes('gemini')) return 'speaker-gemini';
  if (lower.includes('grok')) return 'speaker-grok';
  if (lower.includes('host')) return 'speaker-host';
  return 'speaker-other';
}

// Map a free-form speaker label ("[round 1 · grok-4.3]" /
// "[xai/grok-4.3]" / "[claude/claude-opus-4-7]") down to a stable key
// used to dedupe streaming-vs-final transcript blocks. The streaming
// label format from utils/stream_progress.py is "<provider>/<model>";
// the panelist_answer label format from tools/panel.py is "round N ·
// <agent>". We match on the same set of model substrings the
// speakerClass function uses for colouring.
function speakerKeyForDedupe(label) {
  const lower = String(label || '').toLowerCase();
  if (lower.startsWith('judge')) return 'judge';
  if (lower.includes('codex') || lower.includes('openai') || lower.includes('gpt')) return 'codex';
  if (lower.includes('gemini')) return 'gemini';
  if (lower.includes('grok') || lower.includes('xai')) return 'grok';
  if (lower.includes('claude') || lower.includes('anthropic')) return 'claude';
  if (lower.includes('host')) return 'host';
  return null;
}

function _extractLabel(msg) {
  const m = String(msg || '').match(/^\[([^\]]+)\]/);
  return m ? m[1] : '';
}

function flattenTranscriptEvents(node, out) {
  // Walk the run tree once collecting transcript-renderable events,
  // then do a second pass to suppress streaming blocks whose speaker
  // already has a final panelist_answer in scope.
  //
  // Three event types feed the transcript pane:
  //   panelist_answer / judge_synthesis — final, authoritative
  //   text_chunk — provider streaming progress; aggregated per node
  //                into one transcript block.
  //
  // Per-node aggregation: if the node has chunks, synthesize one
  // panelist_streaming block. The streaming block stays baked into the
  // transcript even AFTER the node completes (so the user keeps seeing
  // the model's words during the gap before the canonical
  // panelist_answer fires) — but the dedupe pass below removes it once
  // a panelist_answer for the same speaker has actually arrived.
  const collected = [];
  _walkCollect(node, collected);

  // Build the set of speaker keys that have a final answer somewhere
  // in this tree.
  const finalKeys = new Set();
  for (const e of collected) {
    if (e.event_type === 'panelist_answer' || e.event_type === 'judge_synthesis') {
      const k = speakerKeyForDedupe(_extractLabel(e.message));
      if (k) finalKeys.add(k);
    }
  }

  // Emit, suppressing redundant streaming blocks. A panelist_streaming
  // whose speaker key already has a final answer is pure noise — the
  // panelist_answer event carries the full content authoritatively.
  for (const e of collected) {
    if (e.event_type === 'panelist_streaming') {
      const k = speakerKeyForDedupe(_extractLabel(e.message));
      if (k && finalKeys.has(k)) continue;
    }
    out.push(e);
  }
  return out;
}

function _walkCollect(node, out) {
  if (node.events) {
    const chunks = [];
    for (const e of node.events) {
      if (e.event_type === 'panelist_answer' || e.event_type === 'judge_synthesis') {
        out.push(e);
      } else if (e.event_type === 'text_chunk') {
        chunks.push(e);
      }
    }
    if (chunks.length) {
      let label = '';
      const bodyParts = [];
      for (const c of chunks) {
        const m = String(c.message || '').match(/^\[([^\]]+)\]\s*([\s\S]*)$/);
        if (m) {
          if (!label) label = m[1];
          bodyParts.push(m[2]);
        }
      }
      const isRunning = node.status === 'running';
      out.push({
        event_type: 'panelist_streaming',
        ts: chunks[chunks.length - 1].ts,
        message: `[${label}] ${isRunning ? '(writing live…)' : '(streamed)'}\n${bodyParts.join('')}`,
        _streaming_running: isRunning,
      });
    }
  }
  for (const c of (node.children || [])) _walkCollect(c, out);
}

function hasAnyStatusActivity(node) {
  if (node.status === 'running') return true;
  for (const c of (node.children || [])) {
    if (hasAnyStatusActivity(c)) return true;
  }
  return false;
}

function renderTranscriptEvent(e) {
  const msg = e.message || '';
  const splitAt = msg.indexOf('\n');
  const header = splitAt > 0 ? msg.slice(0, splitAt) : msg;
  const body = splitAt > 0 ? msg.slice(splitAt + 1) : '';
  // Speaker label is the bracketed prefix.
  const m = header.match(/^\[([^\]]+)\]\s*(.*)$/);
  const speakerKey = m ? m[1] : header;
  const tail = m ? m[2] : '';
  let cls;
  if (e.event_type === 'judge_synthesis') {
    cls = 'transcript transcript-judge speaker-judge';
  } else if (e.event_type === 'panelist_streaming') {
    // In-progress streaming — distinct visual treatment so the operator
    // can tell they're watching live writing, not the final answer.
    cls = 'transcript transcript-streaming ' + speakerClass(speakerKey);
  } else {
    cls = 'transcript ' + speakerClass(speakerKey);
  }
  return `<div class="${cls}">
    <div class="transcript-h">
      <span class="who">${escapeHtml(speakerKey)}</span>
      ${tail ? `<span>${escapeHtml(tail)}</span>` : ''}
      <span class="when">${escapeHtml(fmtTime(e.ts))}</span>
    </div>
    <div class="transcript-body">${escapeHtml(body)}</div>
  </div>`;
}

function renderRawTree(tree) {
  return `<div class="raw-tree"><pre>${escapeHtml(JSON.stringify(tree, null, 2))}</pre></div>`;
}

function renderVerdictTally(tree) {
  // Pull the panel's verdict_tally from the root run's result if available.
  let tally = null;
  try {
    if (tree.result_json) {
      const parsed = JSON.parse(tree.result_json);
      let candidate = parsed.verdict_tally;
      if (!candidate && parsed.result && Array.isArray(parsed.result) && parsed.result[0]) {
        try { candidate = JSON.parse(parsed.result[0]).verdict_tally; } catch (_) {}
      }
      tally = candidate;
    }
  } catch (_) {}
  if (!tally) return '';
  const chips = Object.entries(tally).map(([k, v]) =>
    `<span class="v-${safeClass(k)}">${escapeHtml(String(v))}× ${escapeHtml(k)}</span>`).join('');
  return `<span class="verdict-tally">${chips}</span>`;
}

// Walk the tree to find the deepest-running descendant — that's the run
// whose status the user actually cares about, even when the dispatcher
// (multiaudit / start_task) returned in 60ms.
function effectiveRunStatus(tree) {
  // If anything in the tree is still running, the panel is still going.
  let hasRunning = false;
  function walk(n) {
    if (n.status === 'running') hasRunning = true;
    for (const c of (n.children || [])) walk(c);
  }
  walk(tree);
  if (hasRunning) return 'running';
  return tree.status;
}

// Pick a representative tool name — prefer the deepest non-dispatcher
// child so we say "panel" rather than "multiaudit · 60ms".
function effectiveToolName(tree) {
  const dispatchers = new Set(['multiaudit', 'start_task']);
  let best = tree;
  function walk(n) {
    if (!dispatchers.has(n.tool_name)) best = n;
    for (const c of (n.children || [])) walk(c);
  }
  walk(tree);
  return best;
}

let LAST_EVENT_COUNT = 0;
// "Sticky bottom" auto-scroll. We don't try to remember intent across
// renders — we just check, AT RENDER TIME, whether the user is near the
// bottom of the page. If yes, follow new content. If they've scrolled up
// even a little, leave them alone — they're reading history. The
// previous 600px/200px hysteresis flag fought the user when they
// scrolled mid-stream.
const STICKY_BOTTOM_PX = 100;

async function renderConversation() {
  if (!SELECTED) {
    $('#content').innerHTML = '<div class="empty">waiting for a panel run…</div>';
    return;
  }
  try {
    const r = await fetch('/runs/' + SELECTED + '/tree');
    if (!r.ok) {
      $('#content').innerHTML = '<div class="empty">run not found</div>';
      return;
    }
    const body = await r.json();
    const tree = body.tree;
    const events = flattenTranscriptEvents(tree, []);
    events.sort((a, b) => (a.ts || 0) - (b.ts || 0));

    // Header reflects the deepest running descendant so a 60ms dispatcher
    // doesn't show "completed" while the actual panel is still streaming.
    const effStatus = effectiveRunStatus(tree);
    const effRun = effectiveToolName(tree);
    const effElapsed = fmtElapsed(effRun.started_at, effRun.completed_at);

    const summary = `<div class="summary">
      ${statusBadge(effStatus)}
      <span style="margin-left:8px;">${escapeHtml(effRun.tool_name || '')}</span>
      <span style="color:var(--muted);margin-left:8px;">${escapeHtml(effRun.label || tree.label || '')}</span>
      <span style="color:var(--muted);margin-left:8px;">· ${escapeHtml(effElapsed)}</span>
      ${renderVerdictTally(tree)}
    </div>`;

    let middle;
    if (events.length) {
      middle = events.map(renderTranscriptEvent).join('');
      if (effStatus === 'running') {
        middle += `<div class="thinking">panelists still talking…</div>`;
      }
    } else if (effStatus === 'running' || hasAnyStatusActivity(tree)) {
      middle = `<div class="thinking">panelists are thinking — first answer arriving soon…</div>`;
    } else {
      middle = `<div class="empty">no transcript events for this run<div class="hint">this run didn't go through panel — try multiaudit or panel directly</div></div>`;
    }

    // Sample scroll position BEFORE replacing innerHTML — that's our
    // signal for whether to follow new content. Replacing innerHTML can
    // shift the document height, so we decide based on pre-render state.
    const preDistFromBottom = (
      document.body.scrollHeight - (window.innerHeight + window.scrollY)
    );
    const wasAtBottom = preDistFromBottom <= STICKY_BOTTOM_PX;
    const preScrollY = window.scrollY;

    $('#content').innerHTML = summary + middle + renderRawTree(tree);

    const newEventCount = events.length;
    LAST_EVENT_COUNT = newEventCount;
    if (wasAtBottom) {
      // User was at/near the bottom — follow the latest line.
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'auto' });
    } else {
      // User scrolled up to read history. Pin them in place. Browsers
      // mostly preserve scrollY across innerHTML replacement when the
      // upstream layout doesn't change drastically, but explicitly
      // restoring guarantees no snap-back when streaming text grows the
      // page below them.
      window.scrollTo({ top: preScrollY, behavior: 'auto' });
    }
  } catch (e) {
    $('#content').innerHTML = '<div class="empty">error: ' + escapeHtml(e.message) + '</div>';
  }
}

function statusBadge(status) {
  return `<span class="badge b-${safeClass(status)}">${escapeHtml(status || '')}</span>`;
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}
function escapeAttr(s) { return escapeHtml(s); }
function safeClass(s) {
  return String(s == null ? '' : s).replace(/[^a-zA-Z0-9_-]/g, '_');
}

// -- wiring ---------------------------------------------------------------
$('#run-picker').addEventListener('change', (e) => {
  SELECTED = e.target.value;
  MANUAL_PICK = true;
  LAST_EVENT_COUNT = 0;
  window.scrollTo({ top: 0 });
  renderConversation();
});

$('#raw-toggle').addEventListener('click', () => {
  RAW_MODE = !RAW_MODE;
  document.body.classList.toggle('raw', RAW_MODE);
  $('#raw-toggle').classList.toggle('active', RAW_MODE);
});

renderRunPicker();
setInterval(renderRunPicker, 2000);
setInterval(() => { if (SELECTED) renderConversation(); }, 1500);
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

            # Bounded + validated limit. Pre-fix, int() ran outside try/except
            # so a single GET /runs?limit=abc crashed the daemon thread; and
            # ?limit=-1 reached SQLite as `LIMIT -1` = unbounded dump.
            try:
                raw_limit = qs.get("limit", ["50"])[0]
                limit = int(raw_limit)
            except (ValueError, TypeError):
                self._send_json(
                    {"status": "error", "error": f"invalid 'limit' parameter: {raw_limit!r}"},
                    status=400,
                )
                return
            if limit < 1 or limit > 200:
                self._send_json(
                    {"status": "error", "error": "'limit' must be between 1 and 200"},
                    status=400,
                )
                return

            status_filter = (qs.get("status", [None])[0]) or None
            if status_filter is not None and status_filter not in (
                "running", "completed", "failed", "cancelled"
            ):
                self._send_json(
                    {"status": "error", "error": f"invalid 'status' filter: {status_filter!r}"},
                    status=400,
                )
                return

            tool_name = (qs.get("tool_name", [None])[0]) or None
            try:
                rows = graph.list_runs(limit=limit, status=status_filter, tool_name=tool_name)
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

    # Opt-in gate for non-localhost binds. See _ALLOW_REMOTE comment up
    # top: the viewer is unauthenticated, so we refuse to expose it
    # beyond localhost without an explicit env opt-in.
    if _BIND_HOST not in _LOCALHOST_BINDS and not _ALLOW_REMOTE:
        logger.warning(
            "Refusing to start web viewer on %s — non-localhost binds expose "
            "the full execution graph (prompts, responses, diffs) without "
            "auth. Set PAL_WEB_ALLOW_REMOTE=1 to override.",
            _BIND_HOST,
        )
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
            # new=0: reuse an existing browser tab/window when possible.
            # new=2 (force-new-tab) caused proliferation across PAL
            # restarts — every Claude Code restart spawned another tab
            # the user had to hunt down. With new=0 the browser brings
            # an existing localhost:<port> tab to the foreground and
            # refreshes it, so the operator keeps a single canonical
            # viewer tab for as long as PAL grabs the same port.
            opened = webbrowser.open(url, new=0, autoraise=False)
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
