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
import re
import time
from typing import Any, Optional

from mcp.types import TextContent

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool
from utils.progress import emit_progress

logger = logging.getLogger("pal.panel")

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
# Code itself when PAL is running under it. Routed through MCP sampling
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
                "context. This usually means PAL was invoked outside a real MCP "
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
    # host see PAL's tools/resources during sampling — usually wanted for
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
) -> dict[str, Any]:
    """Run a single panelist. Returns a structured per-panelist outcome."""
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

    await emit_progress(f"panel/{label}: dispatching", progress=0.0)

    try:
        # Host-LLM panelist: route through MCP sampling instead of clink/chat.
        # Lets Claude Code (or any MCP host with sampling support) be a true
        # peer in the debate without spawning a subprocess or hitting a paid
        # API. The host's tokens + cost are the host's problem; PAL just
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
        await emit_progress(f"panel/{label}: ✗ error", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": False,
            "duration_s": duration,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _truncate(text: str, *, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = text[: cap - 80]
    return head + f"\n…[panel: truncated {len(text) - cap + 80:,} chars]"


def _build_judge_prompt(original_prompt: str, panelist_results: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    sections.append(
        "You are synthesizing a panel of AI models that each independently answered a question. "
        "Identify points of agreement, points of divergence, the strongest argument from each, "
        "and your overall recommendation."
    )
    sections.append(
        "\nCRITICAL OUTPUT FORMAT — your response MUST begin with a fenced HEADLINE block, exactly:\n"
        "<HEADLINE>\n"
        "[2-3 sentences plain English. Direct verdict on the question. Lead with the answer. "
        "If panelists converged, name the consensus. If they diverged, name the divergence and your call. "
        "No preamble, no 'after careful consideration', just the verdict.]\n"
        "</HEADLINE>\n"
        "Then continue with your full reasoning under normal prose. Callers read the HEADLINE alone "
        "for a quick scan; the body is for provenance and depth."
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
        "panelists are independent AI models that answered the same question. "
        "Your goal in this round: critique their positions, defend yours where "
        "they disagree, concede where they convinced you, and produce a revised "
        "answer. Be specific and concrete — reference the other panelists by "
        "label when you respond to their points."
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
        "Write your updated position now. Lead with where you changed your mind "
        "(if anywhere), then where you still disagree and why."
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
                    "from an internal tool that marks its payload as PAL-generated."
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
                    )
                judge_outcome["duration_s"] = round(time.monotonic() - judge_started, 2)
                # Extract the leading <HEADLINE> the judge was asked to write.
                if judge_outcome.get("ok"):
                    headline = _extract_headline(judge_outcome.get("response", ""))
                    if headline:
                        judge_outcome["headline"] = headline
                judge_result = judge_outcome

        # Surface the judge's headline at the top level so callers can scan
        # the verdict without parsing nested judge.response JSON.
        top_headline: Optional[str] = None
        if isinstance(judge_result, dict):
            top_headline = judge_result.get("headline")

        payload: dict[str, Any] = {
            "status": panel_status,
            "headline": top_headline,
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
