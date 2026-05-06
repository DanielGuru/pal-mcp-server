"""Tests for the 'host' panel agent — MCP sampling path.

Stubs the MCP session so we never actually call out to a host LLM. Verifies:
  - host name routes through _run_host_panelist (not clink/chat)
  - returns ok=True with cost_tier='host_sampling' when session works
  - returns clear errors when session is missing / capability missing /
    timeout / host raises / empty content
"""

from __future__ import annotations

import asyncio
import pytest


def _stub_session(*, response_text: str = "OK from host", supports_sampling: bool = True,
                  raises: Exception | None = None, slow: bool = False):
    """Build a fake session compatible with the bits panel/host_session use."""

    class _Result:
        def __init__(self, text):
            class _Content:
                def __init__(self, t):
                    self.text = t
            self.content = _Content(text)
            self.model = "claude-stub"

    class _Session:
        async def create_message(self, **kwargs):
            if slow:
                await asyncio.sleep(2.0)  # exceed timeout in tests
            if raises is not None:
                raise raises
            return _Result(response_text)

        def check_client_capability(self, cap):
            return supports_sampling

    return _Session()


def _run(agent, label="host", timeout=1.0):
    from tools.panel import _run_host_panelist
    import time
    return asyncio.run(_run_host_panelist(
        agent=agent, label=label, role="default",
        prompt="Hi", timeout=timeout, started=time.monotonic(),
    ))


def test_host_panelist_returns_response_with_sampling_cost_tier(monkeypatch):
    import utils.host_session as hs

    monkeypatch.setattr(hs, "get_host_session", lambda: _stub_session())
    out = _run("host")
    assert out["ok"] is True
    assert out["cost_tier"] == "host_sampling"
    assert "OK from host" in out["response"]
    assert out["host_model"] == "claude-stub"


def test_host_panelist_fails_when_no_session(monkeypatch):
    import utils.host_session as hs
    monkeypatch.setattr(hs, "get_host_session", lambda: None)
    out = _run("host")
    assert out["ok"] is False
    assert "no MCP session is reachable" in out["error"]


def test_host_panelist_fails_when_sampling_unsupported(monkeypatch):
    import utils.host_session as hs
    monkeypatch.setattr(hs, "get_host_session", lambda: _stub_session(supports_sampling=False))
    out = _run("host")
    assert out["ok"] is False
    assert "sampling" in out["error"].lower()


def test_host_panelist_handles_session_exception(monkeypatch):
    import utils.host_session as hs
    monkeypatch.setattr(
        hs, "get_host_session",
        lambda: _stub_session(raises=RuntimeError("upstream gateway 502")),
    )
    out = _run("host")
    assert out["ok"] is False
    assert "host sampling failed" in out["error"]
    assert "RuntimeError" in out["error"]


def test_host_panelist_times_out_cleanly(monkeypatch):
    import utils.host_session as hs
    monkeypatch.setattr(hs, "get_host_session", lambda: _stub_session(slow=True))
    out = _run("host", timeout=0.1)
    assert out["ok"] is False
    assert "timed out" in out["error"]


def test_host_panelist_rejects_empty_content(monkeypatch):
    import utils.host_session as hs
    monkeypatch.setattr(hs, "get_host_session", lambda: _stub_session(response_text=""))
    out = _run("host")
    assert out["ok"] is False
    assert "empty content" in out["error"]


def test_host_panelist_handles_mixed_content_blocks(monkeypatch):
    """MCP allows the host to return multiple content blocks (text + image
    + audio). Pre-fix the panel handler stringified the list and produced
    garbage. Now: extract .text from every text-shaped block, ignore the
    rest, return the concatenated text."""
    import utils.host_session as hs

    class _TextBlock:
        def __init__(self, t):
            self.text = t
            self.type = "text"

    class _ImageBlock:
        def __init__(self):
            self.type = "image"
            self.data = "base64..."  # no .text

    class _Result:
        def __init__(self):
            self.content = [
                _TextBlock("First the analysis."),
                _ImageBlock(),  # ignored, not stringified into the response
                _TextBlock("Then the conclusion."),
            ]
            self.model = "claude-stub"

    class _Session:
        async def create_message(self, **kwargs):
            return _Result()

        def check_client_capability(self, cap):
            return True

    monkeypatch.setattr(hs, "get_host_session", lambda: _Session())
    out = _run("host")
    assert out["ok"] is True
    assert "First the analysis." in out["response"]
    assert "Then the conclusion." in out["response"]
    # The image block did not leak its repr into the response
    assert "ImageBlock" not in out["response"]
    assert "base64" not in out["response"]


def test_panel_routing_recognises_host_name():
    """is_host_agent / is_clink_agent must classify 'host' correctly."""
    from tools.panel import _is_clink_agent, _is_host_agent
    assert _is_host_agent("host") is True
    assert _is_host_agent("HOST") is True
    assert _is_host_agent("codex") is False
    # 'host' should NOT also count as clink (would dispatch to subprocess)
    assert _is_clink_agent("host") is False
