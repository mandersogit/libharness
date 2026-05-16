from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import typing as t
import uuid
from dataclasses import dataclass

JsonObject = dict[str, t.Any]
RpcEvent = JsonObject


class PiRpcError(RuntimeError):
    pass


class PiRpcProcessError(PiRpcError):
    pass


@dataclass
class PendingResponse:
    queue: "queue.Queue[JsonObject]"


class PiRpcClient:
    """Strict-JSONL subprocess client for `pi --mode rpc`."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stderr: t.Literal["pipe", "inherit", "devnull"] = "pipe",
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.stderr_mode = stderr
        self.process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._pending: dict[str, PendingResponse] = {}
        self._pending_lock = threading.Lock()
        self.events: "queue.Queue[JsonObject]" = queue.Queue()
        self.stderr_lines: "queue.Queue[str]" = queue.Queue()
        self._closed = threading.Event()

    def start(self) -> "PiRpcClient":
        if self.process is not None:
            return self
        stderr_target: t.Any
        if self.stderr_mode == "pipe":
            stderr_target = subprocess.PIPE
        elif self.stderr_mode == "devnull":
            stderr_target = subprocess.DEVNULL
        else:
            stderr_target = None
        env = os.environ.copy()
        if self.env:
            env.update(self.env)
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            text=False,
            bufsize=0,
        )
        self._reader_thread = threading.Thread(target=self._read_stdout, name="pi-rpc-stdout", daemon=True)
        self._reader_thread.start()
        if self.process.stderr is not None:
            self._stderr_thread = threading.Thread(target=self._read_stderr, name="pi-rpc-stderr", daemon=True)
            self._stderr_thread.start()
        return self

    def close(self, *, terminate_timeout: float = 2.0) -> None:
        self._closed.set()
        proc = self.process
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=terminate_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=terminate_timeout)
        self.process = None

    def send(self, command: JsonObject, *, timeout: float = 30.0) -> JsonObject:
        if self.process is None:
            raise PiRpcError("PiRpcClient.start() has not been called")
        if self.process.poll() is not None:
            raise PiRpcProcessError(f"Pi process already exited with code {self.process.returncode}")
        request_id = str(command.get("id") or uuid.uuid4())
        command = {**command, "id": request_id}
        pending = PendingResponse(queue.Queue(maxsize=1))
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            encoded = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
            assert self.process.stdin is not None
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
            try:
                response = pending.queue.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for Pi RPC response to {command['type']!r}") from exc
            if not response.get("success", True):
                raise PiRpcError(str(response.get("error") or response))
            return response
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def get_state(self, *, timeout: float = 10.0) -> JsonObject:
        return self.send({"type": "get_state"}, timeout=timeout)["data"]

    def get_commands(self, *, timeout: float = 10.0) -> list[JsonObject]:
        return self.send({"type": "get_commands"}, timeout=timeout)["data"]["commands"]

    def get_available_models(self, *, timeout: float = 10.0) -> list[JsonObject]:
        return self.send({"type": "get_available_models"}, timeout=timeout)["data"]["models"]

    def set_model(self, provider: str, model_id: str, *, timeout: float = 10.0) -> JsonObject:
        return self.send({"type": "set_model", "provider": provider, "modelId": model_id}, timeout=timeout)["data"]

    def prompt(self, message: str, *, timeout: float = 10.0, streaming_behavior: str | None = None) -> JsonObject:
        cmd: JsonObject = {"type": "prompt", "message": message}
        if streaming_behavior:
            cmd["streamingBehavior"] = streaming_behavior
        return self.send(cmd, timeout=timeout)

    def wait_event(self, predicate: t.Callable[[JsonObject], bool] | None = None, *, timeout: float = 30.0) -> JsonObject:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Pi RPC event")
            event = self.events.get(timeout=remaining)
            if predicate is None or predicate(event):
                return event

    def drain_events(self) -> list[JsonObject]:
        events: list[JsonObject] = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def drain_stderr(self) -> list[str]:
        lines: list[str] = []
        while True:
            try:
                lines.append(self.stderr_lines.get_nowait())
            except queue.Empty:
                return lines

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while not self._closed.is_set():
            line = self.process.stdout.readline()
            if not line:
                break
            if line.endswith(b"\n"):
                line = line[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                self.events.put({"type": "client_error", "error": f"Invalid JSONL from Pi: {exc}", "raw": line.decode('utf-8', 'replace')})
                continue
            if frame.get("type") == "response" and "id" in frame:
                with self._pending_lock:
                    pending = self._pending.get(str(frame["id"]))
                if pending is not None:
                    pending.queue.put(frame)
                    continue
            self.events.put(frame)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while not self._closed.is_set():
            line = self.process.stderr.readline()
            if not line:
                break
            self.stderr_lines.put(line.decode("utf-8", "replace").rstrip("\n"))
