"""Panel orchestration — fan out one prompt to N models concurrently with optional judge.

This is the headline feature for multi-model orchestration. Where `consensus`
runs models sequentially with shared state, `panel`:

  - Fires every panelist in parallel (asyncio.gather)
  - Routes each panelist through the cheapest path automatically:
      * 'codex' / 'gemini'           -> clink (OAuth, free)
      * any other string             -> chat tool (paid API; Grok lives here)
  - Optionally calls a judge model afterwards with all panelist outputs and
    the original prompt to synthesize divergence/agreement
  - Returns structured JSON: per-panelist response + duration + cost-tier flag,
    plus the judge synthesis if requested

Designed to be wrapped in `start_task` for long audits so the conversation is
not blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

from mcp.types import TextContent

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool
from utils.progress import emit_progress

logger = logging.getLogger("panel.panel")

DEFAULT_TIMEOUT_S = 600
MAX_PANELISTS = 8
MAX_DEBATE_ROUNDS = 3  # additional adversarial rounds after the initial parallel fan-out

# Cap on per-panelist response text included in the judge prompt. Keeps the
# judge's context bounded even if one panelist produces a wall of output.
JUDGE_PER_PANELIST_CHAR_CAP = 8000
# Cap on a peer's response shown to a panelist during adversarial rounds.
# Smaller than the judge cap because each panelist sees N-1 peers; total
# context grows N*(N-1).
DEBATE_PER_PEER_CHAR_CAP = 4000


# Reserved panelist name that means "the MCP host's own LLM" — i.e. Claude
# Code itself when Panel is running under it. Routed through MCP sampling
# (mcp/createMessage), not clink and not chat. Counts as a peer panelist
# in the debate; the host model sees the prompt and answers like any other.
HOST_AGENT_NAME = "host"


def _is_host_agent(name: str) -> bool:
    """Match the literal panelist name 'host' (the MCP-side LLM)."""
    return name.lower() == HOST_AGENT_NAME


def _is_clink_agent(name: str) -> bool:
    """Decide whether `name` should route through clink.

    Derived from clink's runtime registry rather than a hard-coded set, so
    adding a new clink CLI in conf/cli_clients/ makes panel route to it
    automatically. Excludes the reserved 'host' name (sampling, not clink)
    even when a clink config happens to be named that.
    """
    if _is_host_agent(name):
        return False
    try:
        from clink.registry import get_registry
        return name.lower() in {n.lower() for n in get_registry().list_clients()}
    except Exception:  # noqa: BLE001
        # Conservative fallback if the registry isn't importable.
        return name.lower() in {"codex", "gemini", "claude"}


def _derive_cost_tier(initial_tier: str, response_text: str) -> str:
    """Determine the actual cost tier from the panelist's response metadata.

    The dispatch-time tier is a guess based on the requested agent:
      - clink-routed agents (codex, gemini): 'oauth_free'
      - direct-API agents (grok, gpt-5.5):   'api_paid'

    But clink can transparently fall back to the paid API when OAuth fails
    (TerminalQuotaError, 401, etc. — see tools/clink.py F1 fallback). When
    that happens, the response metadata carries oauth_fallback_used=true.
    Reading that here is the only way the panel result can honestly report
    what was actually billed.

    Implementation note: prior version substring-matched the rendered JSON,
    which was both spoofable (a model emitting the literal phrase) and
    fragile to whitespace formatting. This version parses the response as
    structured JSON and reads the metadata field directly. Falls back to
    the initial tier if the response isn't JSON or doesn't carry the field.
    """
    import json

    if initial_tier != "oauth_free" or not response_text:
        return initial_tier

    # Inner tools return a JSON-serialised ToolOutput; if there are multiple
    # TextContent chunks we joined them with "\n", so try the first chunk
    # first then the whole blob.
    candidates = [response_text]
    if "\n" in response_text:
        candidates.insert(0, response_text.split("\n", 1)[0])
    for blob in candidates:
        blob = blob.strip()
        if not blob.startswith("{"):
            continue
        try:
            payload = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("oauth_fallback_used") is True:
            return "oauth_fallback_paid"
        # Found valid JSON but no fallback marker — no need to try more.
        break

    return initial_tier


def _normalize_panelist(entry: Any) -> dict[str, Any]:
    """Accept either a string or an object spec; return a normalized dict."""
    if isinstance(entry, str):
        return {"agent": entry}
    if isinstance(entry, dict):
        return dict(entry)
    raise ValueError(f"Each panelist must be a string or object, got {type(entry).__name__}")


async def _run_host_panelist(
    *,
    agent: str,
    label: str,
    role: str,
    prompt: str,
    timeout: float,
    started: float,
) -> dict[str, Any]:
    """Dispatch the prompt to the MCP host LLM (Claude Code) via sampling.

    Returns the same per-panelist outcome shape as _run_panelist's regular
    path, with cost_tier='host_sampling' (host eats the cost, not us).

    Failure modes (each returns ok=False with a clear error message):
      - No session reachable in this context (running in a non-MCP test,
        or the captured session has been torn down).
      - Host doesn't advertise the sampling capability (older clients).
      - The host rejects / errors on the create_message call.
      - Timeout — the host's model took longer than `timeout` seconds.
    """
    from utils.host_session import get_host_session, host_supports_sampling

    session = get_host_session()
    if session is None:
        await emit_progress(f"panel/{label}: ✗ host sampling unavailable", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": False,
            "duration_s": round(time.monotonic() - started, 2),
            "error": (
                "host sampling unavailable: no MCP session is reachable in this "
                "context. This usually means Panel was invoked outside a real MCP "
                "client request, or the captured session was torn down before "
                "the panel ran."
            ),
        }

    if not host_supports_sampling(session):
        await emit_progress(
            f"panel/{label}: ✗ host doesn't support sampling", progress=1.0
        )
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": False,
            "duration_s": round(time.monotonic() - started, 2),
            "error": (
                "host LLM did not advertise the 'sampling' capability during "
                "the MCP handshake. Use a host that supports sampling (Claude "
                "Code does) or pick a different panelist."
            ),
        }

    # MCP createMessage takes a list of SamplingMessages. We pass the panel
    # prompt as a single user turn. include_context='thisServer' lets the
    # host see Panel's tools/resources during sampling — usually wanted for
    # PR-shaped audits where the host might want to peek at the diff.
    try:
        from mcp.types import SamplingMessage, TextContent as MCPTextContent
    except Exception as exc:  # noqa: BLE001
        return {
            "agent": agent, "label": label, "role": role, "ok": False,
            "duration_s": round(time.monotonic() - started, 2),
            "error": f"mcp.types import failed: {exc}",
        }

    sampling_msg = SamplingMessage(
        role="user",
        content=MCPTextContent(type="text", text=prompt),
    )

    try:
        result = await asyncio.wait_for(
            session.create_message(
                messages=[sampling_msg],
                max_tokens=4096,
                include_context="thisServer",
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await emit_progress(f"panel/{label}: ✗ host timed out", progress=1.0)
        return {
            "agent": agent, "label": label, "role": role, "ok": False,
            "duration_s": round(time.monotonic() - started, 2),
            "error": f"host sampling timed out after {timeout}s",
        }
    except Exception as exc:  # noqa: BLE001
        await emit_progress(f"panel/{label}: ✗ host error", progress=1.0)
        return {
            "agent": agent, "label": label, "role": role, "ok": False,
            "duration_s": round(time.monotonic() - started, 2),
            "error": f"host sampling failed: {type(exc).__name__}: {exc}",
        }

    # Extract the text content from the host's response. SamplingMessage's
    # content is a single block; if it's text, take .text — otherwise stringify.
    # We check whether `.text` exists (could be empty string) before falling
    # back to str(); empty .text means the host genuinely returned nothing.
    # MCP allows the host to return either a single content block OR a list
    # of mixed content blocks (text + image + audio). We handle both:
    # extract text from every text-shaped block, concatenate, ignore the rest.
    content = getattr(result, "content", None)
    if content is None:
        response_text = ""
    elif isinstance(content, list):
        # Mixed-content response: pull .text from each text block.
        text_chunks: list[str] = []
        for block in content:
            block_text = getattr(block, "text", None)
            if block_text:
                text_chunks.append(block_text)
        response_text = "\n".join(text_chunks)
    elif hasattr(content, "text"):
        response_text = content.text or ""
    else:
        response_text = str(content)
    if not response_text.strip():
        return {
            "agent": agent, "label": label, "role": role, "ok": False,
            "duration_s": round(time.monotonic() - started, 2),
            "error": "host returned empty content",
        }

    duration = round(time.monotonic() - started, 2)

    # Record the host response as a child run in the execution graph so
    # `run_tree(panel_run_id)` can recover the full text the same way it
    # recovers clink/chat panelists. Without this, summary_only=true would
    # tell callers "full responses are in the graph" — but the host's path
    # bypasses execute_tool, so its run never gets created. Audit-flagged
    # ("Data-loss bug on host panelist"), gemini severity=blocker.
    try:
        from utils.execution_graph import current_run_id, get_graph

        graph = get_graph()
        parent = current_run_id()
        if graph is not None and parent:
            host_run_id = graph.start_run(
                tool_name="host_sampling",
                label=f"panelist:{label}",
                parent_run_id=parent,
                args={"prompt_chars": len(prompt), "host_model": getattr(result, "model", None)},
            )
            graph.complete_run(
                host_run_id,
                result={"response": response_text, "host_model": getattr(result, "model", None)},
                cost_tier="host_sampling",
                model_used=getattr(result, "model", None),
            )
    except Exception:  # noqa: BLE001
        # Graph writes are best-effort — never fail the panelist on graph
        # error. The response text is still in the result dict below.
        logger.debug("host panelist graph recording failed", exc_info=True)

    await emit_progress(f"panel/{label}: ✓ host responded ({duration}s)", progress=1.0)
    return {
        "agent": agent,
        "label": label,
        "role": role,
        "ok": True,
        "cost_tier": "host_sampling",
        "duration_s": duration,
        "response": response_text,
        "host_model": getattr(result, "model", None),
    }


async def _run_panelist(
    panelist: dict[str, Any],
    *,
    prompt: str,
    files: list[str],
    images: list[str],
    timeout: float,
    inject_schema_suffix: bool = True,
) -> dict[str, Any]:
    """Run a single panelist. Returns a structured per-panelist outcome.

    By default appends the structured-tail schema instruction so callers can
    extract `verdict`/`headline`/`key_findings` reliably. Set
    `inject_schema_suffix=False` for the judge call (which already follows
    its own HEADLINE-only schema) and any other prompt that arrives
    pre-formatted.
    """
    agent = panelist.get("agent")
    if not isinstance(agent, str) or not agent:
        return {
            "agent": str(agent),
            "ok": False,
            "error": "panelist 'agent' must be a non-empty string",
        }

    role = panelist.get("role") or "default"
    label = panelist.get("label") or agent
    is_host = _is_host_agent(agent)
    is_clink = _is_clink_agent(agent)
    started = time.monotonic()

    if inject_schema_suffix:
        prompt = with_panelist_schema(prompt)

    await emit_progress(f"panel/{label}: dispatching", progress=0.0)

    try:
        # Host-LLM panelist: route through MCP sampling instead of clink/chat.
        # Lets Claude Code (or any MCP host with sampling support) be a true
        # peer in the debate without spawning a subprocess or hitting a paid
        # API. The host's tokens + cost are the host's problem; Panel just
        # routes the prompt through mcp/createMessage.
        if is_host:
            return await _run_host_panelist(
                agent=agent,
                label=label,
                role=role,
                prompt=prompt,
                timeout=timeout,
                started=started,
            )

        # Pre-flight provider availability check for paid-API agents. Without
        # this, a missing XAI_API_KEY shows up as "panel/grok-4.3: ✗ error"
        # in the activity feed with no hint that the fix is "set XAI_API_KEY"
        # — users see a generic failure and don't know it's a config issue.
        # Rule: no OAuth path → must have an API provider; if neither, fail
        # fast with an actionable message instead of dispatching to chat
        # only to have it raise the same thing wrapped in panel's generic
        # exception handler.
        if not is_clink:
            from providers.registry import ModelProviderRegistry

            if ModelProviderRegistry.get_provider_for_model(agent) is None:
                duration = round(time.monotonic() - started, 2)
                hint = _provider_setup_hint(agent)
                error_msg = (
                    f"no provider configured for model {agent!r}. {hint} "
                    f"(or pass panelists=[...] to drop this agent from the panel)."
                )
                await emit_progress(
                    f"panel/{label}: ✗ {error_msg}", progress=1.0
                )
                return {
                    "agent": agent,
                    "label": label,
                    "role": role,
                    "ok": False,
                    "duration_s": duration,
                    "error": error_msg,
                }

        # Dispatch through server.execute_tool so panelists get the same
        # validation as MCP-boundary calls (model resolution, file-size cap).
        # Pre-fix, panel was a quiet way to bypass MCP-boundary validation.
        from server import execute_tool

        if is_clink:
            tool_name = "clink"
            args = {
                "prompt": prompt,
                "cli_name": agent,
                "role": role,
                "absolute_file_paths": files,
                "images": images,
                # Hint the graph layer that this dispatch is initially
                # OAuth-free; if clink falls back, it'll override.
                "_graph_cost_tier": "oauth_free",
                "_graph_label": f"panelist:{label}",
            }
            initial_tier = "oauth_free"
        else:
            tool_name = "chat"
            args = {
                "prompt": prompt,
                "model": agent,
                "absolute_file_paths": files,
                "images": images,
                "working_directory_absolute_path": panelist.get("working_directory_absolute_path") or "/tmp",
                "_graph_cost_tier": "api_paid",
                "_graph_label": f"panelist:{label}",
            }
            initial_tier = "api_paid"

        result = await asyncio.wait_for(execute_tool(tool_name, args), timeout=timeout)
        duration = round(time.monotonic() - started, 2)
        # tool.execute returns list[TextContent]; concatenate text
        text_parts = [getattr(item, "text", str(item)) for item in (result or [])]
        response_text = "\n".join(text_parts)
        # cost_tier derived from response metadata so OAuth→API fallback is
        # honestly reported (otherwise paid runs are mislabelled 'oauth_free').
        cost_tier = _derive_cost_tier(initial_tier, response_text)
        await emit_progress(f"panel/{label}: ✓ done ({duration}s, {cost_tier})", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": True,
            "cost_tier": cost_tier,
            "duration_s": duration,
            "response": response_text,
        }
    except asyncio.TimeoutError:
        duration = round(time.monotonic() - started, 2)
        await emit_progress(f"panel/{label}: ✗ timed out", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": False,
            "duration_s": duration,
            "error": f"timed out after {timeout}s",
        }
    except Exception as exc:  # noqa: BLE001
        duration = round(time.monotonic() - started, 2)
        # Surface the underlying exception in the progress event, not just
        # "✗ error". A user staring at the live activity feed needs to see
        # WHY a panelist failed (no provider / quota / timeout chain / etc.)
        # without having to dig into task_result.
        snippet = f"{type(exc).__name__}: {exc}"
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        await emit_progress(f"panel/{label}: ✗ {snippet}", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": False,
            "duration_s": duration,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _provider_setup_hint(model_name: str) -> str:
    """Human-friendly hint for which env var to set when a model has no
    provider. Used by the pre-flight dispatch check so users see the fix
    inline in the panel activity feed.
    """

    lower = model_name.lower()
    if lower.startswith("grok-") or "grok" in lower:
        return "Set XAI_API_KEY (xAI Grok has no OAuth path; API only)."
    if lower.startswith("gpt-") or lower.startswith("o3"):
        return "Set OPENAI_API_KEY (or use the 'codex' clink agent for free OAuth via ChatGPT subscription)."
    if lower.startswith("claude-") or lower == "claude":
        return "Set ANTHROPIC_API_KEY (or use the 'claude' clink agent for free OAuth via Claude subscription)."
    if lower.startswith("gemini-") or lower == "gemini":
        return "Set GEMINI_API_KEY (or use the 'gemini' clink agent for free OAuth via Google account)."
    return (
        "Configure a provider for this model: ANTHROPIC_API_KEY / "
        "OPENAI_API_KEY / GEMINI_API_KEY / XAI_API_KEY / OPENROUTER_API_KEY / "
        "CUSTOM_API_URL — then restart your MCP client."
    )


def _truncate(text: str, *, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = text[: cap - 80]
    return head + f"\n…[panel: truncated {len(text) - cap + 80:,} chars]"


# Cap on the answer body streamed to the viewer per panelist. Set high
# enough that real panelist responses (~5-8KB) survive intact — the user
# wants the FULL conversation, not summaries. The graph layer's event-row
# cap (PANEL_GRAPH_EVENT_CAP, default 32KB) is the real ceiling; this is
# just a defensive upper bound against pathological essays.
TRANSCRIPT_BODY_CAP = 24000


def _unwrap_chat_envelope(text: str) -> str:
    """Strip the chat-tool JSON envelope so the user sees prose, not JSON.

    Clink and chat tools return responses shaped like
    `{"status":"continuation_available","content":"...\\n..."}`. Useful
    machine-readable, but in a transcript view the user just wants the
    prose — they don't want to read `\\n` escapes or curly braces.
    Defensive: if the payload isn't a recognisable envelope, return the
    original text unchanged.
    """
    if not text:
        return text
    s = text.lstrip()
    if not (s.startswith("{") and '"content"' in s[:200]):
        return text
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
        return parsed["content"]
    return text


async def _emit_panelist_answer(
    *,
    label: str,
    role: str,
    response_text: str,
    kind: str,
    round_num: Optional[int] = None,
) -> None:
    """Stream a panelist's answer body to the live viewer.

    The viewer renders these as transcript blockquotes — that's what makes
    the live feed read as a real conversation between the models, not just
    a pile of status pings. The structured tail is stripped, the chat-tool
    JSON envelope is unwrapped, so the message contains only readable prose.

    `kind`:
      - "answer"            — round 1 (initial position)
      - "debate"            — round 2+ (revised position after seeing peers)
      - "judge"             — judge synthesis
    """
    body = _strip_tail_blocks(_unwrap_chat_envelope(response_text))
    if len(body) > TRANSCRIPT_BODY_CAP:
        body = body[:TRANSCRIPT_BODY_CAP] + "…"
    if not body.strip():
        return  # nothing useful to show

    if kind == "judge":
        prefix = f"[judge:{label}]"
        event_type = "judge_synthesis"
    elif kind == "debate":
        prefix = f"[round {round_num} · {label}] (revised)"
        event_type = "panelist_answer"
    else:
        prefix = f"[round 1 · {label}]"
        event_type = "panelist_answer"

    await emit_progress(
        f"{prefix}\n{body}",
        progress=0.0,
        event_type=event_type,
    )


def _build_judge_prompt(original_prompt: str, panelist_results: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    sections.append(
        "You are the judge synthesising a panel of independent AI models that "
        "each answered the same question. The user is going to act on what you "
        "say. Be decisive. Your job is NOT to average — it's to weigh evidence "
        "and call which panelists were right where they disagreed.\n"
        "\n"
        "How to weigh panelists:\n"
        "- A panelist who showed code (`file:line` cites, actual diffs) outweighs "
        "one who reasoned in the abstract.\n"
        "- A panelist who tagged confidence honestly (especially LOW where "
        "warranted) is more trustworthy than one who claimed HIGH everywhere.\n"
        "- Convergence across independent models is signal, but a single "
        "panelist with concrete evidence outweighs three panelists with "
        "matching vibes. Don't follow majority vote when one model showed its "
        "work.\n"
        "- Call out OVERCLAIMS: if a panelist asserted something HIGH-confidence "
        "without evidence to back it, name that and discount it.\n"
        "- Call out CONVERGENCE-ON-WRONG: if multiple panelists agreed but "
        "missed something the diff/code clearly shows, say so.\n"
    )
    sections.append(
        "\nCRITICAL OUTPUT FORMAT — your response MUST begin with a fenced HEADLINE block, exactly:\n"
        "<HEADLINE>\n"
        "[2-3 sentences plain English. The user's takeaway. Lead with the "
        "verdict, not the process. If panelists converged, name the consensus "
        "and any dissent worth heeding. If they diverged, name your call and "
        "the single strongest reason for it. No 'after careful consideration', "
        "no 'the panel believes', just the answer.]\n"
        "</HEADLINE>\n"
        "Then continue with your full reasoning under normal prose. The body should:\n"
        "1. Identify the points of real disagreement (not surface differences in "
        "phrasing) and explain who you sided with and why.\n"
        "2. Highlight the strongest finding from each panelist that you "
        "incorporated, by panelist label.\n"
        "3. Flag any finding from any panelist that you REJECTED — by name, with "
        "the reason (no evidence, contradicted by another panelist's code cite, "
        "out of scope, etc.).\n"
        "4. End with **RECOMMENDED ACTIONS** — a numbered list, ordered by "
        "priority, of what the user should do. Concrete and actionable. Not "
        '"consider testing more"; "add `tests/test_x.py::test_xauth_drop` '
        'asserting Y; ship the fix from panelist Z\'s diff".'
    )
    sections.append("\n=== ORIGINAL QUESTION ===\n" + original_prompt.strip())
    for r in panelist_results:
        if r.get("ok"):
            response = (r.get("response") or "").strip()
            response = _truncate(response, cap=JUDGE_PER_PANELIST_CHAR_CAP)
            sections.append(
                f"\n=== PANELIST: {r['agent']} (role={r.get('role')}, {r.get('duration_s')}s) ===\n{response}"
            )
        else:
            sections.append(f"\n=== PANELIST: {r['agent']} — FAILED: {r.get('error')} ===")
    sections.append("\n=== YOUR SYNTHESIS (start with <HEADLINE>...</HEADLINE>) ===\n")
    return "\n".join(sections)


_HEADLINE_RE = re.compile(r"<HEADLINE>(.+?)</HEADLINE>", re.IGNORECASE | re.DOTALL)


def _extract_headline(judge_response: str) -> str | None:
    """Pull the 2-3 sentence headline the judge was asked to lead with."""
    if not judge_response:
        return None
    m = _HEADLINE_RE.search(judge_response)
    if not m:
        return None
    return m.group(1).strip() or None


# ---------------------------------------------------------------------------
# Per-panelist structured-output schema
#
# Default panel result was a 50–80kB blob with each panelist's full essay
# inlined as a single string field. Two problems: (a) blew the MCP response
# size cap, forcing callers to spawn subagents to summarise; (b) the host
# LLM couldn't address individual findings without re-reading everything.
#
# Fix: ask each panelist to END their response with fenced structured-tail
# blocks (same trick as the judge's <HEADLINE>). The full prose stays in the
# graph (each panelist is its own run record reachable via run_tree); the
# default `task_result` payload now returns a parsed `summary` per panelist
# plus an excerpt, keeping responses small enough to read inline.
# ---------------------------------------------------------------------------

# Excerpt cap on full response when returned in summary mode.
SUMMARY_RESPONSE_EXCERPT_CHARS = 600

# Sentinel anchoring the structured tail. Without this, an unanchored regex
# would happily match `<VERDICT>...</VERDICT>` *inside reviewed code or a PR
# diff* the panelist quoted in their answer — the audit caught this as a
# security finding ("PR authors could spoof verdicts via untrusted diffs").
# We require the panelist to emit this exact sentinel before any tag block;
# parsing only considers text after the LAST occurrence so even a panelist
# who quotes the schema mid-prose gets the right answer.
_TAIL_SENTINEL = "<<<PANEL_STRUCTURED_TAIL_v1>>>"

_PANELIST_SCHEMA_SUFFIX = (
    "\n\n---\n"
    "OUTPUT FORMAT — when you have finished your analysis, append the following "
    "fenced structured-tail blocks (in this order). Use them in addition to your "
    "normal prose; do not let them replace your reasoning. Tags MUST be "
    "uppercase. Empty/inapplicable blocks may be omitted EXCEPT <VERDICT> and "
    "<HEADLINE> which are required.\n"
    "\n"
    f"Begin the structured tail with the literal sentinel `{_TAIL_SENTINEL}` on "
    "its own line — this anchors the parser. Anything BEFORE the sentinel is "
    "treated as your prose and not parsed for tags. Place this sentinel + the "
    "tag blocks at the very end of your response.\n"
    "\n"
    f"{_TAIL_SENTINEL}\n"
    "<VERDICT>land | needs-changes | reject</VERDICT>\n"
    "<SEVERITY>blocker | major | minor | nit</SEVERITY>\n"
    "<HEADLINE>One sentence (max 200 chars). Direct, no preamble.</HEADLINE>\n"
    "<KEY_FINDINGS>\n"
    "- [bug|design|security|test_gap|drift|nit] one finding per line, optional path:line ref\n"
    "</KEY_FINDINGS>\n"
    "<FILES_TO_PRESERVE>\n"
    "- path/to/file.py — reason\n"
    "</FILES_TO_PRESERVE>\n"
    "<FILES_TO_BACKFILL>\n"
    "- name_or_path — reason\n"
    "</FILES_TO_BACKFILL>\n"
    "<RECOMMENDED_ACTIONS>\n"
    "- imperative bullet (start with a verb)\n"
    "</RECOMMENDED_ACTIONS>\n"
)


def with_panelist_schema(prompt: str) -> str:
    """Append the structured-tail schema to a panelist's prompt."""
    return prompt.rstrip() + _PANELIST_SCHEMA_SUFFIX


_TAIL_BLOCK_RE = re.compile(
    r"<(VERDICT|SEVERITY|HEADLINE|KEY_FINDINGS|FILES_TO_PRESERVE|FILES_TO_BACKFILL|RECOMMENDED_ACTIONS)>"
    r"(.*?)"
    r"</\1>",
    re.IGNORECASE | re.DOTALL,
)


def _isolate_structured_tail(response_text: str) -> str:
    """Return the substring AFTER the last occurrence of the tail sentinel.

    This is the security boundary for tail-block parsing: anything before the
    last sentinel is the panelist's prose (which may quote the schema) and
    must not contribute parsed tags. If the sentinel is absent the panelist
    didn't follow the schema; return empty so no garbage is parsed.
    """
    if not response_text:
        return ""
    idx = response_text.rfind(_TAIL_SENTINEL)
    if idx < 0:
        return ""
    return response_text[idx + len(_TAIL_SENTINEL):]
_VALID_VERDICTS = {"land", "needs-changes", "reject"}
_VALID_SEVERITIES = {"blocker", "major", "minor", "nit"}


def _split_bullets(block: str) -> list[str]:
    """Split a bullet-list block into clean string entries."""
    out: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("-", "*", "•")):
            line = line[1:].lstrip()
        if line.startswith(tuple(f"{n}." for n in range(10))):
            line = line.split(".", 1)[1].lstrip()
        if line:
            out.append(line)
    return out


def extract_panelist_summary(response_text: str) -> dict[str, Any]:
    """Parse the structured-tail blocks a panelist was instructed to emit.

    Returns a summary dict. Always includes `parse_complete` (True iff at
    least VERDICT and HEADLINE were extracted). Missing / unrecognised blocks
    are silently elided so a slightly-non-compliant model still yields a
    partial summary.

    Only the substring AFTER the last `_TAIL_SENTINEL` is considered — this
    prevents tag-shaped content quoted in the panelist's prose (e.g. from a
    PR diff under review) from spoofing the verdict.
    """
    blocks: dict[str, str] = {}
    tail = _isolate_structured_tail(response_text)
    if tail:
        for m in _TAIL_BLOCK_RE.finditer(tail):
            tag = m.group(1).upper()
            content = m.group(2).strip()
            blocks[tag] = content

    summary: dict[str, Any] = {}
    verdict = blocks.get("VERDICT", "").strip().lower()
    if verdict in _VALID_VERDICTS:
        summary["verdict"] = verdict
    elif verdict:
        # Try to coerce e.g. "Needs Changes" → "needs-changes"
        coerced = verdict.replace(" ", "-").replace("_", "-")
        if coerced in _VALID_VERDICTS:
            summary["verdict"] = coerced

    severity = blocks.get("SEVERITY", "").strip().lower()
    if severity in _VALID_SEVERITIES:
        summary["severity"] = severity

    headline = blocks.get("HEADLINE", "").strip()
    if headline:
        # Cap to one line, hard limit.
        summary["headline"] = headline.split("\n", 1)[0].strip()[:280]

    for tag, key in (
        ("KEY_FINDINGS", "key_findings"),
        ("FILES_TO_PRESERVE", "files_to_preserve"),
        ("FILES_TO_BACKFILL", "files_to_backfill"),
        ("RECOMMENDED_ACTIONS", "recommended_actions"),
    ):
        block = blocks.get(tag, "")
        if block:
            bullets = _split_bullets(block)
            if bullets:
                summary[key] = bullets

    summary["parse_complete"] = "verdict" in summary and "headline" in summary
    return summary


def _strip_tail_blocks(response_text: str) -> str:
    """Return the response body with the structured tail removed.

    Used to build a clean excerpt of the panelist's prose without echoing
    the structured tail (which is already parsed into `summary`). Splits at
    the sentinel — everything after the LAST sentinel occurrence is the
    structured tail and gets dropped. If the sentinel is absent we fall
    back to the broader regex strip so partially-compliant responses still
    yield a clean excerpt.
    """
    text = response_text or ""
    idx = text.rfind(_TAIL_SENTINEL)
    if idx >= 0:
        return text[:idx].rstrip()
    return _TAIL_BLOCK_RE.sub("", text).strip()


def panelist_summary_view(result: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded-size view of a panelist outcome.

    Drops the full `response` text in favour of `response_chars` +
    `response_excerpt` + parsed `summary`. The full text is preserved in
    the execution graph (each panelist's `execute_tool` call is its own
    run record, queryable via `run_tree(panel_run_id)` or `get_run`).
    """
    view: dict[str, Any] = {
        "agent": result.get("agent"),
        "label": result.get("label"),
        "role": result.get("role"),
        "ok": bool(result.get("ok")),
        "duration_s": result.get("duration_s"),
    }
    if result.get("cost_tier"):
        view["cost_tier"] = result["cost_tier"]
    if result.get("error"):
        view["error"] = result["error"]

    response_text = result.get("response") or ""
    if response_text:
        view["response_chars"] = len(response_text)
        body = _strip_tail_blocks(response_text)
        if len(body) > SUMMARY_RESPONSE_EXCERPT_CHARS:
            view["response_excerpt"] = body[:SUMMARY_RESPONSE_EXCERPT_CHARS] + "…"
            view["response_excerpt_truncated"] = True
        else:
            view["response_excerpt"] = body
            view["response_excerpt_truncated"] = False
        view["summary"] = extract_panelist_summary(response_text)

    return view


def _panel_status(panelists_ok: int, panelists_total: int) -> str:
    if panelists_ok == 0:
        return "failed"
    if panelists_ok < panelists_total:
        return "partial"
    return "completed"


def _build_debate_prompt(
    original_prompt: str,
    self_label: str,
    self_previous: str,
    peers: list[dict[str, Any]],
) -> str:
    """Construct an adversarial round-N prompt for one panelist.

    `peers` is a list of {label, response} for the OTHER panelists' last-round
    output. This panelist's own previous response is shown separately so they
    can revise it.
    """
    sections: list[str] = []
    sections.append(
        "You are a panelist in an adversarial multi-model debate. The other "
        "panelists are independent AI models that saw the same question and "
        "answered it without knowing what you said. Your job in this round is "
        "NOT to summarise — it's to engage. The judge will synthesise; if "
        "you're hand-wavy you get out-voted by panelists who took clear "
        "positions backed by evidence."
    )
    sections.append("\n=== ORIGINAL QUESTION ===\n" + original_prompt.strip())
    if self_previous.strip():
        sections.append(
            f"\n=== YOUR PREVIOUS ANSWER (you are '{self_label}') ===\n"
            + _truncate(self_previous.strip(), cap=JUDGE_PER_PANELIST_CHAR_CAP)
        )
    if peers:
        for peer in peers:
            sections.append(
                f"\n=== PEER PANELIST: {peer['label']} ===\n"
                + _truncate((peer.get("response") or "").strip(), cap=DEBATE_PER_PEER_CHAR_CAP)
            )
    else:
        sections.append("\n=== PEER PANELISTS ===\n(no peer outputs available)")
    sections.append(
        "\n=== YOUR REVISED ANSWER ===\n"
        "Required structure for this round:\n"
        "\n"
        "1. **PEER ENGAGEMENT** — for EACH peer above, by label, write one of:\n"
        "   - `CONCEDE [peer-label]:` <one line on the specific finding they "
        "saw that you missed, and how it changes your position>\n"
        "   - `COUNTER [peer-label]:` <specific reason their finding is wrong, "
        "overstated, or out of scope — cite the evidence they're misreading>\n"
        "   `Mostly agree` / `partially agree` is not acceptable. You must "
        "concede or counter their single strongest finding by name.\n"
        "\n"
        "2. **REVISED POSITION** — your updated answer. Lead with what changed "
        "from round 1 (if anything). Then your final position with the same "
        "rigour as round 1: cite `file:line` if reviewing code, tag confidence, "
        "show diffs/code rather than describing them. If your position didn't "
        "move, say so explicitly and explain why peer arguments didn't shift "
        "you — that itself is signal.\n"
        "\n"
        "Do not pad. Do not restate peer positions back to them — engage with "
        "the strongest point and move on. Do not hedge."
    )
    return "\n".join(sections)


async def _run_debate_round(
    *,
    round_num: int,
    panelists: list[dict[str, Any]],
    last_round_results: list[dict[str, Any]],
    original_prompt: str,
    files: list[str],
    images: list[str],
    timeout: float,
) -> list[dict[str, Any]]:
    """Run a single adversarial round in parallel. Returns one outcome per panelist.

    Only panelists that succeeded in the previous round participate; failures
    propagate forward as failures (we don't ask a model to debate when it
    couldn't answer the original question).
    """
    # Index last-round results by label for peer lookups.
    by_label = {r["label"]: r for r in last_round_results}

    async def _one(panelist: dict[str, Any]) -> dict[str, Any]:
        label = panelist.get("label") or panelist.get("agent")
        prior = by_label.get(label)
        if prior is None or not prior.get("ok"):
            # Carry forward failure verbatim.
            return prior or {
                "agent": panelist.get("agent"),
                "label": label,
                "ok": False,
                "error": "no prior round result to carry forward",
            }
        peers = [
            {"label": r["label"], "response": r.get("response", "")}
            for r in last_round_results
            if r["label"] != label and r.get("ok")
        ]
        debate_prompt = _build_debate_prompt(
            original_prompt=original_prompt,
            self_label=label,
            self_previous=prior.get("response", ""),
            peers=peers,
        )
        await emit_progress(f"panel/round-{round_num}: dispatching {label}", progress=0.0)
        # Mark internal: panel just BUILT this debate prompt from peer
        # responses, so size-check gates should bypass on the inner
        # chat/clink dispatch.
        from tools.shared.base_tool import mark_internal_payload
        with mark_internal_payload():
            return await _run_panelist(
                panelist,
                prompt=debate_prompt,
                files=files,
                images=images,
                timeout=timeout,
            )

    tasks = [asyncio.create_task(_one(p), name=f"debate-r{round_num}:{p.get('label')}") for p in panelists]
    results: list[dict[str, Any]] = []
    finished = 0
    try:
        for fut in asyncio.as_completed(tasks):
            outcome = await fut
            results.append(outcome)
            finished += 1
            tag = "✓" if outcome.get("ok") else "✗"
            await emit_progress(
                f"panel/round-{round_num}: {tag} {outcome.get('label')} ({finished}/{len(panelists)})",
                progress=float(finished),
                total=float(len(panelists)),
            )
            # Stream the revised answer for the live transcript.
            if outcome.get("ok") and outcome.get("response"):
                await _emit_panelist_answer(
                    label=outcome.get("label", "?"),
                    role=outcome.get("role", "default"),
                    response_text=outcome["response"],
                    kind="debate",
                    round_num=round_num,
                )
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    # Order results to match input panelist order so callers see consistent positions.
    by_label_out = {r.get("label"): r for r in results}
    ordered = []
    for p in panelists:
        lbl = p.get("label") or p.get("agent")
        ordered.append(by_label_out.get(lbl) or {"agent": p.get("agent"), "label": lbl, "ok": False, "error": "missing result"})
    return ordered


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class PanelTool(BaseTool):
    """Fan out one prompt to multiple AI models concurrently, optionally judged."""

    def get_name(self) -> str:
        return "panel"

    def get_description(self) -> str:
        return (
            "Fan out the same prompt to multiple AI models in parallel and return "
            "structured per-model responses. Each panelist named 'codex' or 'gemini' "
            "is routed through clink (OAuth, free). Other names go through chat as "
            "paid API model strings (e.g. 'grok-4.3', 'gpt-5.5'). Optionally specify "
            "a 'judge' (any agent name) which receives all panelist outputs and "
            "synthesizes agreement / divergence / recommendation. Use start_task to "
            "wrap this for long audits so the conversation isn't blocked."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The question or task to put to every panelist verbatim.",
                },
                "panelists": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PANELISTS,
                    "description": (
                        "List of agents to consult in parallel. Each entry is either a string "
                        "(e.g. 'codex', 'gemini', 'grok-4.3', 'gpt-5.5') OR an object with "
                        "{agent, role?, label?}. Total max %d panelists." % MAX_PANELISTS
                    ),
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "agent": {"type": "string"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["default", "codereviewer", "planner"],
                                    },
                                    "label": {"type": "string"},
                                },
                                "required": ["agent"],
                                "additionalProperties": True,
                            },
                        ],
                    },
                },
                "judge": {
                    "type": "string",
                    "description": (
                        "Optional agent name to synthesize the panel. If set, after all "
                        "panelists complete the judge receives the original prompt + all "
                        "panelist outputs and produces a synthesis. Use 'codex' or 'gemini' "
                        "for free OAuth synthesis; any other name uses paid API."
                    ),
                },
                "absolute_file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to share with every panelist.",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional image paths shared with every panelist.",
                },
                "panelist_timeout_s": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 1800,
                    "description": "Per-panelist timeout in seconds (default 600).",
                },
                "debate_rounds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_DEBATE_ROUNDS,
                    "description": (
                        "Number of additional adversarial rounds AFTER the initial parallel fan-out. "
                        "0 = no debate (default). Each round: every panelist sees the others' "
                        "previous responses and is asked to critique, defend, and revise. The "
                        "judge synthesizes the FINAL round (with full history available)."
                    ),
                },
                "summary_only": {
                    "type": "boolean",
                    "description": (
                        "When true (default), the result returns a parsed `summary` per panelist "
                        "(verdict, severity, headline, key_findings, files_to_preserve, "
                        "files_to_backfill, recommended_actions) plus a short `response_excerpt` — "
                        "keeping the payload small enough to read inline. Full panelist responses "
                        "are preserved in the execution graph; retrieve them via "
                        "`run_tree(panel_run_id)` or `get_run(child_run_id)`. Set false to inline "
                        "the full text (legacy behaviour; can blow MCP response size limits)."
                    ),
                },
            },
            "required": ["prompt", "panelists"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> Optional[dict[str, Any]]:
        return {"readOnlyHint": False, "openWorldHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.EXTENDED_REASONING

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        # ----- argument parsing -----
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _err("'prompt' must be a non-empty string")

        # Boundary size check on the user's prompt — bypassed when an
        # internal generator (multiaudit) marked the context, fired
        # otherwise. Audit panel finding: direct `panel(prompt=<huge>)`
        # used to fan out to N panelists with no boundary check at all.
        from tools.shared.base_tool import is_internal_payload
        if not is_internal_payload():
            from config import MCP_PROMPT_SIZE_LIMIT
            if len(prompt) > MCP_PROMPT_SIZE_LIMIT:
                return _err(
                    f"panel 'prompt' too large: {len(prompt):,} characters "
                    f"(limit {MCP_PROMPT_SIZE_LIMIT:,}). Save the long content to "
                    "a file and pass via absolute_file_paths, or invoke panel "
                    "from an internal tool that marks its payload as Panel-generated."
                )

        raw_panelists = arguments.get("panelists")
        if not isinstance(raw_panelists, list) or not raw_panelists:
            return _err("'panelists' must be a non-empty list")
        if len(raw_panelists) > MAX_PANELISTS:
            return _err(f"too many panelists ({len(raw_panelists)}); max is {MAX_PANELISTS}")

        try:
            panelists = [_normalize_panelist(p) for p in raw_panelists]
        except ValueError as exc:
            return _err(str(exc))

        # Auto-suffix duplicate labels. The debate-round peer-lookup uses
        # by_label dicts (panel.py ~290 / ~552) and would silently drop one
        # entry if two panelists shared a label, corrupting which peer
        # critiqued whom. Audit finding from gpt-5.5.
        seen_labels: dict[str, int] = {}
        for p in panelists:
            base = p.get("label") or p.get("agent") or "panelist"
            if base in seen_labels:
                seen_labels[base] += 1
                p["label"] = f"{base}#{seen_labels[base]}"
            else:
                seen_labels[base] = 1
                p["label"] = base

        files = arguments.get("absolute_file_paths") or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            return _err("'absolute_file_paths' must be a list of strings")
        images = arguments.get("images") or []
        if not isinstance(images, list) or not all(isinstance(i, str) for i in images):
            return _err("'images' must be a list of strings")

        timeout = arguments.get("panelist_timeout_s")
        if timeout is None:
            timeout = float(DEFAULT_TIMEOUT_S)
        else:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                return _err("'panelist_timeout_s' must be numeric")
            if timeout < 5 or timeout > 1800:
                return _err("'panelist_timeout_s' must be between 5 and 1800")

        judge = arguments.get("judge")
        if judge is not None and (not isinstance(judge, str) or not judge.strip()):
            return _err("'judge' must be a non-empty string when provided")

        debate_rounds_raw = arguments.get("debate_rounds", 0)
        if isinstance(debate_rounds_raw, bool) or not isinstance(debate_rounds_raw, int):
            return _err("'debate_rounds' must be an integer")
        if debate_rounds_raw < 0 or debate_rounds_raw > MAX_DEBATE_ROUNDS:
            return _err(f"'debate_rounds' must be between 0 and {MAX_DEBATE_ROUNDS}")
        debate_rounds = debate_rounds_raw

        summary_only_raw = arguments.get("summary_only", True)
        if not isinstance(summary_only_raw, bool):
            return _err("'summary_only' must be a boolean")
        summary_only = summary_only_raw

        # ----- fan out (streaming via as_completed) -----
        await emit_progress(
            f"panel: dispatching to {len(panelists)} panelists in parallel",
            progress=0.0,
        )
        started = time.monotonic()
        panelist_tasks = [
            asyncio.create_task(
                _run_panelist(p, prompt=prompt, files=files, images=images, timeout=timeout),
                name=f"panelist:{p.get('label') or p.get('agent')}",
            )
            for p in panelists
        ]
        panelist_results: list[dict[str, Any]] = []
        finished = 0
        try:
            for fut in asyncio.as_completed(panelist_tasks):
                outcome = await fut
                panelist_results.append(outcome)
                finished += 1
                tag = "✓" if outcome.get("ok") else "✗"
                await emit_progress(
                    f"panel: {tag} {outcome.get('label')} ({finished}/{len(panelists)})",
                    progress=float(finished),
                    total=float(len(panelists) + (1 if judge else 0)),
                )
                # Stream the answer body itself to the viewer so the live
                # feed reads as a panel transcript, not just status pings.
                if outcome.get("ok") and outcome.get("response"):
                    await _emit_panelist_answer(
                        label=outcome.get("label", "?"),
                        role=outcome.get("role", "default"),
                        response_text=outcome["response"],
                        kind="answer",
                        round_num=1,
                    )
        except asyncio.CancelledError:
            for t in panelist_tasks:
                if not t.done():
                    t.cancel()
            raise
        round1_duration = round(time.monotonic() - started, 2)

        # Order round-1 results to match input panelist order for stable history.
        by_label_r1 = {r.get("label"): r for r in panelist_results}
        round1_ordered = []
        for p in panelists:
            lbl = p.get("label") or p.get("agent")
            round1_ordered.append(by_label_r1.get(lbl) or {"agent": p.get("agent"), "label": lbl, "ok": False, "error": "missing"})

        # ----- adversarial debate rounds -----
        debate_history: list[dict[str, Any]] = [{"round": 1, "panelists": round1_ordered, "duration_s": round1_duration}]
        last_round = round1_ordered

        for r in range(2, 2 + debate_rounds):
            ok_in_round = sum(1 for x in last_round if x.get("ok"))
            if ok_in_round < 2:
                # Need at least 2 successful peers for meaningful debate.
                await emit_progress(
                    f"panel: skipping round {r} — only {ok_in_round} successful panelist(s)",
                    progress=float(len(panelists)),
                )
                break
            await emit_progress(f"panel: starting adversarial round {r}/{1 + debate_rounds}", progress=0.0)
            round_started = time.monotonic()
            this_round = await _run_debate_round(
                round_num=r,
                panelists=panelists,
                last_round_results=last_round,
                original_prompt=prompt,
                files=files,
                images=images,
                timeout=timeout,
            )
            debate_history.append({
                "round": r,
                "panelists": this_round,
                "duration_s": round(time.monotonic() - round_started, 2),
            })
            last_round = this_round

        # Use the FINAL round's results as the canonical panelist outputs.
        panelist_results = last_round
        panel_duration = round(time.monotonic() - started, 2)
        ok_count = sum(1 for r in panelist_results if r.get("ok"))
        panel_status = _panel_status(ok_count, len(panelist_results))

        await emit_progress(
            f"panel: {ok_count}/{len(panelist_results)} succeeded ({panel_status}) over {len(debate_history)} round(s) in {panel_duration}s",
            progress=float(len(panelists)),
            total=float(len(panelists) + (1 if judge else 0)),
        )

        # ----- optional judge synthesis -----
        judge_result: Optional[dict[str, Any]] = None
        if judge:
            if ok_count == 0:
                # Nothing useful to synthesize — skip the judge.
                judge_result = {
                    "agent": judge,
                    "ok": False,
                    "error": "skipped: 0 panelists produced output",
                }
            else:
                judge_panelist = {"agent": judge, "label": f"judge:{judge}", "role": "default"}
                judge_prompt = _build_judge_prompt(prompt, panelist_results)
                judge_started = time.monotonic()
                await emit_progress(
                    f"panel: invoking judge ({judge})",
                    progress=float(len(panelists)),
                    total=float(len(panelists) + 1),
                )
                # Judge prompt is panel-built (synthesised from panelist
                # outputs) — mark internal so size-check gates bypass.
                from tools.shared.base_tool import mark_internal_payload
                with mark_internal_payload():
                    judge_outcome = await _run_panelist(
                        judge_panelist,
                        prompt=judge_prompt,
                        files=[],  # judge sees the panelist outputs, not the original files
                        images=[],
                        timeout=timeout,
                        inject_schema_suffix=False,  # judge has its own HEADLINE-only schema
                    )
                judge_outcome["duration_s"] = round(time.monotonic() - judge_started, 2)
                # Extract the leading <HEADLINE> the judge was asked to write.
                if judge_outcome.get("ok"):
                    headline = _extract_headline(judge_outcome.get("response", ""))
                    if headline:
                        judge_outcome["headline"] = headline
                    # Stream the judge synthesis to the viewer transcript.
                    await _emit_panelist_answer(
                        label=judge,
                        role="judge",
                        response_text=judge_outcome.get("response", ""),
                        kind="judge",
                    )
                judge_result = judge_outcome

        # Surface the judge's headline at the top level so callers can scan
        # the verdict without parsing nested judge.response JSON.
        top_headline: Optional[str] = None
        if isinstance(judge_result, dict):
            top_headline = judge_result.get("headline")

        # Pull the panel's own run_id (set by server.execute_tool when this
        # call began) so callers can fetch the full graph with run_tree.
        try:
            from utils.execution_graph import current_run_id
            panel_run_id = current_run_id()
        except Exception:  # noqa: BLE001
            panel_run_id = None

        if summary_only:
            # Bounded view: parsed structured summary per panelist + excerpt.
            # Full responses live in the execution graph (one run per
            # panelist sub-call) and are reachable via run_tree / get_run.
            view_panelists = [panelist_summary_view(r) for r in panelist_results]
            # Aggregate the verdict counts for a quick at-a-glance reading.
            verdict_tally: dict[str, int] = {}
            for v in view_panelists:
                vd = (v.get("summary") or {}).get("verdict")
                if vd:
                    verdict_tally[vd] = verdict_tally.get(vd, 0) + 1

            view_judge: Optional[dict[str, Any]] = None
            if judge_result is not None:
                if judge_result.get("ok"):
                    judge_response = judge_result.get("response") or ""
                    view_judge = {
                        "agent": judge_result.get("agent"),
                        "ok": True,
                        "duration_s": judge_result.get("duration_s"),
                        "headline": judge_result.get("headline"),
                        "response_chars": len(judge_response),
                    }
                    if len(judge_response) > SUMMARY_RESPONSE_EXCERPT_CHARS * 4:
                        view_judge["response_excerpt"] = (
                            judge_response[: SUMMARY_RESPONSE_EXCERPT_CHARS * 4] + "…"
                        )
                        view_judge["response_excerpt_truncated"] = True
                    else:
                        view_judge["response_excerpt"] = judge_response
                        view_judge["response_excerpt_truncated"] = False
                else:
                    view_judge = {
                        "agent": judge_result.get("agent"),
                        "ok": False,
                        "error": judge_result.get("error"),
                    }

            view_history = None
            if len(debate_history) > 1:
                view_history = [
                    {
                        "round": entry["round"],
                        "duration_s": entry.get("duration_s"),
                        "panelists": [panelist_summary_view(p) for p in entry.get("panelists", [])],
                    }
                    for entry in debate_history
                ]

            payload: dict[str, Any] = {
                "status": panel_status,
                "headline": top_headline,
                "panel_run_id": panel_run_id,
                "panel_duration_s": panel_duration,
                "rounds_run": len(debate_history),
                "panelists_ok": ok_count,
                "panelists_total": len(panelist_results),
                "verdict_tally": verdict_tally or None,
                "panelists": view_panelists,
                "debate_history": view_history,
                "judge": view_judge,
                "full_response_access": (
                    "Full panelist responses are stored in the execution graph. "
                    "Use `run_tree` with this panel_run_id to walk every panelist's "
                    "sub-run (which contains the verbatim response in its result), "
                    "or pass `summary_only=false` to inline the full text in this "
                    "result (warning: can exceed MCP response size limits)."
                ) if panel_run_id else None,
            }
        else:
            # Legacy verbose payload: full panelist responses inlined.
            payload = {
                "status": panel_status,
                "headline": top_headline,
                "panel_run_id": panel_run_id,
                "panel_duration_s": panel_duration,
                "rounds_run": len(debate_history),
                "panelists_ok": ok_count,
                "panelists_total": len(panelist_results),
                "panelists": panelist_results,
                "debate_history": debate_history if len(debate_history) > 1 else None,
                "judge": judge_result,
            }
        return [
            TextContent(
                type="text",
                text=json.dumps(payload, indent=2, default=str),
            )
        ]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"status": "error", "error": message}, indent=2))]


class AskPanelTool(PanelTool):
    """Magic-phrase entry point for the freeform multi-model panel.

    Identical implementation to ``PanelTool`` (same execute, same input
    schema, same orchestration). The split exists for routing clarity:
    ``ask_panel`` is the named entry point Claude Code reaches for when the
    user says "ask the panel", "panel this question", "fan this out", etc.
    The handshake instructions distinguish:

      - ``multiaudit``  — PR/branch-shaped, rigid rubric, auto-diff
      - ``bugfind``     — bug-shaped, rigid rubric, auto-context
      - ``ask_panel``   — freeform, LLM composes the prompt, no rubric

    Internal callers (multiaudit, bugfind, OAuth-first wrapping) keep
    using ``panel`` directly so existing dispatch chains are unaffected.

    Default judge: ``claude-opus-4-7`` (overridable via
    ``PANEL_ASK_PANEL_DEFAULT_JUDGE`` env or per-call ``judge`` arg).
    Reasoning: synthesis work benefits from the strongest writing /
    reasoning model. Multiaudit/bugfind keep ``codex`` as default
    because their rubrics demand cite-the-code rigour over prose
    quality. The caller can always override.
    """

    def get_name(self) -> str:
        return "ask_panel"

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        # Inject default judge ONLY when the caller didn't pass one
        # (or passed an empty string). Don't second-guess explicit
        # caller choice — that's the whole point of the override.
        existing = arguments.get("judge")
        if not (isinstance(existing, str) and existing.strip()):
            default_judge = os.environ.get(
                "PANEL_ASK_PANEL_DEFAULT_JUDGE", "claude-opus-4-7"
            ).strip()
            if default_judge:
                arguments = {**arguments, "judge": default_judge}
        return await super().execute(arguments)

    def get_description(self) -> str:
        return (
            "Freeform multi-model panel — YOU compose the prompt. Use this when "
            "the user says 'ask the panel', 'panel this question', 'fan this "
            "out', 'ask all four about X', 'what does each model think about X', "
            "'second opinion from everyone on X' — anywhere the question is "
            "freestanding (a design call, an architecture question, a 'should "
            "we build it this way' debate) and NOT a PR review or a bug hunt. "
            "For PR/branch reviews use `multiaudit`; for bug investigations "
            "use `bugfind`; for everything else use this. Lift the relevant "
            "context from the conversation into a tight, well-scoped prompt and "
            "pass it as `ask_panel(prompt=..., panelists=[...], judge=..., "
            "debate_rounds=...)`. Don't make the user paste their question "
            "through; you write the prompt. Always wrap in start_task — "
            "panels run for several minutes per round."
        )
