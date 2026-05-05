"""Execute configured CLI agents for the clink tool and parse output."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from clink.constants import DEFAULT_STREAM_LIMIT
from clink.models import ResolvedCLIClient, ResolvedCLIRole
from clink.parsers import BaseParser, ParsedCLIResponse, ParserError, get_parser
from utils.progress import emit_progress

logger = logging.getLogger("clink.agent")

# Limits and tuning knobs (module-level so tests can monkeypatch)
MAX_TOTAL_STREAM_BYTES = 16 * 1024 * 1024  # 16 MB total per stream
PROGRESS_QUEUE_MAXSIZE = 64
PROGRESS_MIN_INTERVAL_S = 0.05
KILL_GRACE_S = 5.0


@dataclass
class AgentOutput:
    """Container returned by CLI agents after successful execution."""

    parsed: ParsedCLIResponse
    sanitized_command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    parser_name: str
    output_file_content: str | None = None


class CLIAgentError(RuntimeError):
    """Raised when a CLI agent fails (non-zero exit, timeout, parse errors)."""

    def __init__(self, message: str, *, returncode: int | None = None, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BaseCLIAgent:
    """Execute a configured CLI command and parse its output."""

    def __init__(self, client: ResolvedCLIClient):
        self.client = client
        self._parser: BaseParser = get_parser(client.parser)
        self._logger = logging.getLogger(f"clink.runner.{client.name}")

    async def run(
        self,
        *,
        role: ResolvedCLIRole,
        prompt: str,
        system_prompt: str | None = None,
        files: Sequence[str],
        images: Sequence[str],
    ) -> AgentOutput:
        # Files and images are already embedded into the prompt by the tool; they are
        # accepted here only to keep parity with SimpleTool callers.
        _ = (files, images)
        command = self._build_command(role=role, system_prompt=system_prompt)
        env = self._build_environment()

        # Resolve executable path for cross-platform compatibility (especially Windows)
        executable_name = command[0]
        resolved_executable = shutil.which(executable_name)
        if resolved_executable is None:
            raise CLIAgentError(
                f"Executable '{executable_name}' not found in PATH for CLI '{self.client.name}'. "
                f"Ensure the command is installed and accessible."
            )
        command[0] = resolved_executable

        sanitized_command = list(command)
        cwd = str(self.client.working_dir) if self.client.working_dir else None
        limit = DEFAULT_STREAM_LIMIT
        start_time = time.monotonic()

        # Optional output-to-file flag handling (cleanup happens in outer finally)
        output_file_path: Path | None = None
        command_with_output_flag = list(command)
        if self.client.output_to_file:
            fd, tmp_path = tempfile.mkstemp(prefix="clink-", suffix=".json")
            os.close(fd)
            output_file_path = Path(tmp_path)
            flag_template = self.client.output_to_file.flag_template
            try:
                rendered_flag = flag_template.format(path=str(output_file_path))
            except KeyError as exc:  # pragma: no cover - defensive
                # Cleanup before re-raising; outer finally won't run because we
                # haven't entered the protected block yet.
                with suppress(OSError):
                    output_file_path.unlink(missing_ok=True)
                raise CLIAgentError(
                    f"Invalid output flag template '{flag_template}': missing placeholder {exc}"
                ) from exc
            command_with_output_flag.extend(shlex.split(rendered_flag))
            sanitized_command = list(command_with_output_flag)

        self._logger.debug("Executing CLI command: %s", " ".join(sanitized_command))
        if cwd:
            self._logger.debug("Working directory: %s", cwd)

        # Spawn subprocess. POSIX: own process group (start_new_session=True) so we
        # can kill the whole tree on timeout/cancel. Windows: fall back to direct kill.
        spawn_kwargs: dict[str, object] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": cwd,
            "limit": limit,
            "env": env,
        }
        if sys.platform != "win32":
            spawn_kwargs["start_new_session"] = True

        try:
            process = await asyncio.create_subprocess_exec(
                *command_with_output_flag, **spawn_kwargs
            )
        except FileNotFoundError as exc:
            if output_file_path is not None:
                with suppress(OSError):
                    output_file_path.unlink(missing_ok=True)
            raise CLIAgentError(f"Executable not found for CLI '{self.client.name}': {exc}") from exc

        progress_queue: asyncio.Queue = asyncio.Queue(maxsize=PROGRESS_QUEUE_MAXSIZE)
        progress_task = asyncio.create_task(
            self._progress_emitter(progress_queue),
            name=f"clink-progress-{self.client.name}",
        )

        # Initial spawn event (drop on full — purely best effort)
        with suppress(asyncio.QueueFull):
            progress_queue.put_nowait((f"{self.client.name}: spawned subprocess", 0.0))

        try:
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            size_state = {"stdout": 0, "stderr": 0}

            async def _write_stdin() -> None:
                if process.stdin is None:
                    return
                try:
                    process.stdin.write(prompt.encode("utf-8"))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    # CLI closed stdin / exited before reading prompt. Don't tear
                    # down the gather — let stdout/stderr drain decide the outcome.
                    pass
                finally:
                    with suppress(Exception):
                        process.stdin.close()

            async def _drain(
                stream: asyncio.StreamReader,
                sink: list[bytes],
                key: str,
                emit: bool,
            ) -> None:
                step = 0
                while True:
                    try:
                        line = await stream.readline()
                    except (asyncio.LimitOverrunError, ValueError):
                        line = await stream.read(limit)
                    if not line:
                        break

                    # Cap accumulated bytes per stream; keep draining (must drain to
                    # avoid stalling the child) but stop appending past the limit.
                    used = size_state[key]
                    if used < MAX_TOTAL_STREAM_BYTES:
                        remaining = MAX_TOTAL_STREAM_BYTES - used
                        if len(line) > remaining:
                            if remaining > 0:
                                sink.append(line[:remaining])
                            sink.append(b"\n[...truncated by clink: stream cap reached...]\n")
                            size_state[key] = MAX_TOTAL_STREAM_BYTES
                        else:
                            sink.append(line)
                            size_state[key] = used + len(line)

                    if not emit:
                        continue

                    # Parser hook is best-effort and should be cheap; never let
                    # an exception in describe_event break the drain loop.
                    try:
                        text = line.decode("utf-8", errors="replace")
                        msg = self._parser.describe_event(text)
                    except Exception:  # noqa: BLE001
                        msg = None
                    if msg:
                        step += 1
                        # Drop on backpressure — keeping drain hot is the priority.
                        with suppress(asyncio.QueueFull):
                            progress_queue.put_nowait((msg, float(step)))

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _write_stdin(),
                        _drain(process.stdout, stdout_chunks, "stdout", emit=True),
                        _drain(process.stderr, stderr_chunks, "stderr", emit=False),
                        process.wait(),
                    ),
                    timeout=self.client.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                await self._terminate_process_tree(process)
                raise CLIAgentError(
                    f"CLI '{self.client.name}' timed out after {self.client.timeout_seconds} seconds",
                    returncode=None,
                ) from exc

            duration = time.monotonic() - start_time
            return_code = process.returncode
            stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            with suppress(asyncio.QueueFull):
                progress_queue.put_nowait(
                    (
                        f"{self.client.name}: complete ({duration:.1f}s, rc={return_code})",
                        999.0,
                    )
                )

            output_file_content: str | None = None
            if output_file_path is not None and output_file_path.exists():
                output_file_content = output_file_path.read_text(encoding="utf-8", errors="replace")
                if output_file_content and not stdout_text.strip():
                    stdout_text = output_file_content

            if return_code != 0:
                recovered = self._recover_from_error(
                    returncode=return_code,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    sanitized_command=sanitized_command,
                    duration_seconds=duration,
                    output_file_content=output_file_content,
                )
                if recovered is not None:
                    return recovered

                raise CLIAgentError(
                    f"CLI '{self.client.name}' exited with status {return_code}",
                    returncode=return_code,
                    stdout=stdout_text,
                    stderr=stderr_text,
                )

            try:
                parsed = self._parser.parse(stdout_text, stderr_text)
            except ParserError as exc:
                raise CLIAgentError(
                    f"Failed to parse output from CLI '{self.client.name}': {exc}",
                    returncode=return_code,
                    stdout=stdout_text,
                    stderr=stderr_text,
                ) from exc

            return AgentOutput(
                parsed=parsed,
                sanitized_command=sanitized_command,
                returncode=return_code,
                stdout=stdout_text,
                stderr=stderr_text,
                duration_seconds=duration,
                parser_name=self._parser.name,
                output_file_content=output_file_content,
            )
        finally:
            # Subprocess cleanup. Runs on success, exception, AND cancellation.
            if process.returncode is None:
                await self._terminate_process_tree(process)
            # Stop progress emitter — drain pending events briefly first.
            progress_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(progress_task, timeout=1.0)
            # Clean up temp output file if requested.
            if (
                output_file_path is not None
                and self.client.output_to_file
                and self.client.output_to_file.cleanup
            ):
                with suppress(OSError):
                    output_file_path.unlink(missing_ok=True)

    async def _progress_emitter(self, queue: asyncio.Queue) -> None:
        """Drain queued progress events and emit MCP notifications with rate limit.

        Keeps the stdout drain loop hot by absorbing bursty parser output and
        emitting at most one notification every PROGRESS_MIN_INTERVAL_S seconds.
        Cancelled by the run() finally block when the subprocess finishes.
        """
        last_emit = 0.0
        while True:
            try:
                msg, progress = await queue.get()
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            wait = PROGRESS_MIN_INTERVAL_S - (now - last_emit)
            if wait > 0:
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    return
            try:
                await emit_progress(msg, progress=progress)
            except Exception:  # noqa: BLE001
                # Transport failure must not leak — the emitter is best-effort.
                pass
            last_emit = time.monotonic()

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        """Kill the subprocess and any descendants on POSIX; fall back on Windows.

        Sends SIGTERM to the process group, waits up to KILL_GRACE_S, then
        escalates to SIGKILL. Bounded — never blocks indefinitely.
        """
        if process.returncode is not None:
            return

        if sys.platform != "win32":
            # Try graceful: SIGTERM the whole process group
            with suppress(ProcessLookupError, PermissionError, OSError):
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_S)
                return
            except asyncio.TimeoutError:
                pass
            # Force: SIGKILL the group
            with suppress(ProcessLookupError, PermissionError, OSError):
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_S)
        else:
            with suppress(ProcessLookupError, OSError):
                process.kill()
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_S)

    def _build_command(self, *, role: ResolvedCLIRole, system_prompt: str | None) -> list[str]:
        base = list(self.client.executable)
        base.extend(self.client.internal_args)
        base.extend(self.client.config_args)
        base.extend(role.role_args)

        return base

    def _build_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.client.env)
        return env

    # ------------------------------------------------------------------
    # Error recovery hooks
    # ------------------------------------------------------------------

    def _recover_from_error(
        self,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        sanitized_command: list[str],
        duration_seconds: float,
        output_file_content: str | None,
    ) -> AgentOutput | None:
        """Hook for subclasses to convert CLI errors into successful outputs.

        Return an AgentOutput to treat the failure as success, or None to signal
        that normal error handling should proceed.
        """

        return None
