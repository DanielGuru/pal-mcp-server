"""Single home for streaming-progress emission from provider worker threads.

Why this exists
---------------
The four direct-API providers (anthropic, openai, xai-via-openai, gemini)
all want to publish per-chunk text to the live execution-graph viewer
while a long generation is in flight. Round-3 audit caught two real bugs
in the first pass at this:

1. **ContextVar drop**: ``providers/base.py`` dispatches sync
   ``generate_content`` to a worker thread via ``loop.run_in_executor``.
   That call does NOT propagate ``contextvars`` (unlike
   ``asyncio.to_thread``), so ``current_run_id()`` returns ``None`` in the
   worker — every emit becomes silent dead code. Fix: callers pass an
   explicit ``run_id``; if not provided we fall back to the ContextVar
   for sync entry points.

2. **Status pings vs content**: the previous helpers emitted
   ``"streaming… (N chunks)"`` strings. The viewer transcript renders
   the message body, so a chunk-count counter never showed text. Fix:
   accumulate text and emit the running content (truncated for safety),
   so the viewer can render the model-in-progress next to the final
   ``panelist_answer``.

Throttling is time-based (not chunk-count) so fast token rates don't
hammer SQLite and slow rates still get reasonable UI updates.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Cap how much accumulated text we ship per chunk event. Keeps individual
# graph rows small even when a model writes a long block; the final
# ``panelist_answer`` event always carries the full response.
DEFAULT_CHUNK_BODY_CAP = 4096
# Min seconds between successive emits per stream. 100ms feels live to a
# human but bounds DB writes to ~10 events/sec/stream.
DEFAULT_THROTTLE_S = 0.1


@dataclass
class StreamProgressEmitter:
    """Per-call helper. Construct once at the top of the streaming loop,
    call ``feed(delta)`` with each new piece of text, then ``finalize()``
    when the stream closes.

    The accumulator owns the rate-limit clock so multiple concurrent
    streams don't share state. ``run_id`` is captured eagerly so a worker
    thread doesn't need to read a ContextVar."""

    label: str
    run_id: Optional[str] = None
    throttle_s: float = DEFAULT_THROTTLE_S
    body_cap: int = DEFAULT_CHUNK_BODY_CAP
    _buffer: list[str] = field(default_factory=list)
    _last_emit: float = 0.0

    def feed(self, delta: str) -> None:
        """Append a text delta and emit if the throttle window allows."""
        if not delta:
            return
        self._buffer.append(delta)
        now = time.monotonic()
        if (now - self._last_emit) >= self.throttle_s:
            self._emit()
            self._last_emit = now

    def finalize(self) -> None:
        """Flush any remaining buffered text. Always emits even if the
        throttle window hasn't elapsed, so the last few tokens reach the
        viewer in the gap before the final ``panelist_answer`` event."""
        if self._buffer:
            self._emit()

    def _emit(self) -> None:
        """Best-effort write to the execution graph against ``run_id``.
        Each emit ships ONLY the new deltas accumulated since the last
        emit, then clears the buffer. Critical: the viewer concats
        successive text_chunk messages, so emitting cumulative content
        would grow each event's body O(N) and DOM size O(N²). Round-3
        panel caught this as a browser-DoS class bug. Swallows everything:
        streaming hot path must not fail because of telemetry."""
        if self.run_id is None:
            self._buffer.clear()
            return
        try:
            from utils.execution_graph import get_graph
            graph = get_graph()
            if graph is None:
                self._buffer.clear()
                return
            content = "".join(self._buffer)
            self._buffer.clear()
            # Tail-slice individual oversized deltas so a single huge
            # chunk doesn't blow the row size. Multi-chunk reconstruction
            # is the viewer's job.
            if len(content) > self.body_cap:
                content = "…" + content[-self.body_cap:]
            graph.add_event(
                self.run_id,
                event_type="text_chunk",
                message=f"[{self.label}] {content}",
                progress=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._buffer.clear()
            logger.debug("stream_progress emit failed: %s", exc)


def make_emitter(label: str, run_id: Optional[str] = None) -> StreamProgressEmitter:
    """Construct an emitter. If ``run_id`` is None we try the active
    ContextVar — works for sync entry points, falls through to a no-op
    when called from a worker thread without explicit ``run_id``."""
    if run_id is None:
        try:
            from utils.execution_graph import current_run_id
            run_id = current_run_id()
        except Exception:  # noqa: BLE001
            run_id = None
    return StreamProgressEmitter(label=label, run_id=run_id)
