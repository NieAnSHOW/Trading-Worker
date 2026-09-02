"""Pi sidecar 进程监管 + RPC 客户端。

设计约束（design §Idempotency）：崩溃最多重启一次并重开活跃 JSONL 会话；
in-flight 请求以 sidecar_restarted 报错（不重放）；重启预算耗尽 → 所有请求
永久 pi_sidecar_unavailable。stdout 是唯一协议流；stderr 必须持续 drain
（否则撑满管道缓冲会阻塞 sidecar），仅保留有界尾部供调试，绝不进入协议流。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Callable

from src.pi_sidecar.protocol import LineSplitter, decode_frame, encode_frame

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SIDECAR_DIR = _REPO_ROOT / "pi-sidecar"

READY_TIMEOUT_S = 30.0


class SidecarError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _default_command() -> list[str]:
    """B7：打包后的 desktop 没有 bun。

    解析顺序：env VIBE_PI_SIDECAR_BIN（存在且可执行 → 直接执行该二进制，Rust 侧
    src-tauri/src/sidecar.rs 注入，Task 15）→ bun dev 命令（源码/开发态）。
    """
    bin_path = os.environ.get("VIBE_PI_SIDECAR_BIN") or ""
    if bin_path and os.path.isfile(bin_path) and os.access(bin_path, os.X_OK):
        return [bin_path]
    return ["bun", "run", "src/main.ts"]


class PiSidecarClient:
    def __init__(
        self,
        *,
        command: list[str] | None = None,
        cwd: Path | None = None,
        agent_dir: Path | None = None,
        sessions_dir: Path | None = None,
        env: dict[str, str] | None = None,  # B6：合并覆盖 os.environ 后传给 sidecar 进程
        on_event: Callable[[str, str, dict], None] | None = None,
        on_host_request: Callable[[str, dict], Any] | None = None,  # B3：async (op, params) -> dict
        max_restarts: int = 1,
    ) -> None:
        self._command = command or _default_command()
        self._cwd = cwd or _SIDECAR_DIR
        self._agent_dir = agent_dir
        self._sessions_dir = sessions_dir
        self._env = env
        self._on_event = on_event
        self._on_host_request = on_host_request
        self._max_restarts = max_restarts
        self._restarts_used = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._splitter = LineSplitter()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._ready = False
        self._unavailable = False
        self._stopping = False
        self._active_sessions: set[str] = set()

    # ---------- lifecycle ----------

    @property
    def started(self) -> bool:
        """ready 帧已收到且进程存活（B4：供 SessionService 探测，不窥探私有字段）。"""
        return self._ready and self._proc is not None and self._proc.returncode is None

    # ---------- lifecycle ----------

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    async def start(self) -> None:
        await self._spawn()

    async def _spawn(self) -> None:
        env = dict(os.environ)
        if self._env:
            env.update(self._env)  # B6：显式 env 覆盖继承环境（集成测试隔离真实 provider 配置）
        if self._agent_dir:
            env["VIBE_PI_AGENT_DIR"] = str(self._agent_dir)
        if self._sessions_dir:
            env["VIBE_PI_SESSIONS_DIR"] = str(self._sessions_dir)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=str(self._cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=2 * 1_048_576,
            )
        except OSError as exc:
            self._mark_unavailable(exc)
            raise SidecarError("pi_sidecar_unavailable", f"spawn failed: {exc}") from exc
        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop())
        # 重开路径（_handle_process_loss → _spawn）：先收掉旧进程的 stderr 任务，
        # 避免旧 pipe 的 readline 悬挂到 loop 关闭（与 stop() 的取消卫生一致）。
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._stderr_task = asyncio.get_running_loop().create_task(self._stderr_drain_loop())
        try:
            await asyncio.wait_for(self._wait_ready(), timeout=READY_TIMEOUT_S)
        except (asyncio.TimeoutError, SidecarError) as exc:
            await self._handle_process_loss(
                exc if isinstance(exc, SidecarError) else SidecarError("timeout", "ready timeout")
            )
            if self._unavailable or self._proc is None:
                raise SidecarError("pi_sidecar_unavailable", "sidecar failed to become ready") from exc

    async def _wait_ready(self) -> None:
        while self._ready is False:
            if self._proc is None or self._proc.returncode is not None:
                raise SidecarError("internal", "sidecar exited before ready")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stderr_task = None
        if proc is not None:
            # 断开 stdin 写管道让 subprocess transport 在 loop 存活时完成收尾，
            # 避免 loop 关闭后 transport 被 GC 触发 "Event loop is closed" unraisable
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
        self._reject_all(SidecarError("internal", "client stopped"))

    # ---------- request/response ----------

    async def request(self, op: str, params: dict, *, timeout: float = 30.0) -> dict:
        if self._unavailable:
            raise SidecarError("pi_sidecar_unavailable", "sidecar restart budget exhausted")
        if self._proc is None or self._proc.returncode is not None:
            await self._handle_process_loss(SidecarError("internal", "sidecar not running"))
            if self._unavailable:
                raise SidecarError("pi_sidecar_unavailable", "sidecar restart budget exhausted")
        self._seq += 1
        req_id = f"r-{self._seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(encode_frame({"v": 1, "id": req_id, "op": op, "params": params}).encode("utf-8"))
            await self._proc.stdin.drain()
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise SidecarError("timeout", f"{op} timed out after {timeout}s") from None
        except (ConnectionResetError, BrokenPipeError) as exc:
            self._pending.pop(req_id, None)
            await self._handle_process_loss(SidecarError("internal", f"pipe lost: {exc}"))
            raise SidecarError("sidecar_restarted", f"{op} lost to sidecar restart") from exc
        except Exception:
            self._pending.pop(req_id, None)
            raise
        if result.get("_error_"):
            err = result["_error_"]
            raise SidecarError(err["code"], err["message"])
        payload = result.get("result", {})
        # open_session / new_session 成功 → 记入活跃集（restart 后重开，B2 配套）
        if op in ("open_session", "new_session") and isinstance(params.get("session_id"), str):
            self._active_sessions.add(str(params["session_id"]))
        return payload  # type: ignore[return-value]

    def _reject_all(self, err: SidecarError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"_error_": {"code": err.code, "message": str(err)}})
        self._pending.clear()

    # ---------- reader ----------

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                chunk = (await proc.stdout.read(65536)).decode("utf-8", "replace")
                if not chunk:
                    break
                for line in self._splitter.push(chunk):
                    self._dispatch_frame(line)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("sidecar reader crashed: %s", exc)
        if not self._stopping:
            await self._handle_process_loss(SidecarError("internal", "sidecar exited"))

    # ---------- stderr drain ----------

    @property
    def stderr_tail(self) -> list[str]:
        """最近有界的 sidecar stderr 诊断行（调试用；stderr 绝不进入协议流）。"""
        return list(self._stderr_tail)

    async def _stderr_drain_loop(self) -> None:
        """持续排空 sidecar stderr，防止撑满管道缓冲（64KB）反向阻塞 sidecar。

        仅保留最近 maxlen 行（deque 有界），进程退出/EOF 即自然结束。
        """
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                self._stderr_tail.append(line.decode("utf-8", "replace").rstrip("\r\n"))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("sidecar stderr drain crashed: %s", exc)

    def _dispatch_frame(self, line: str) -> None:
        try:
            frame = decode_frame(line)
        except Exception as exc:  # noqa: BLE001
            logger.warning("bad frame from sidecar: %s", exc)
            return
        if "event" in frame:
            if frame["event"] == "ready":
                self._ready = True
            if self._on_event:
                self._on_event(
                    str(frame.get("event")), str(frame.get("session_id") or ""), dict(frame.get("data") or {})
                )
            return
        if "op" in frame and "ok" not in frame:
            # B3：sidecar→Python 入站请求（tool_invoke/memory_context/remember/...）
            self._schedule_host_request(str(frame["id"]), str(frame["op"]), dict(frame.get("params") or {}))
            return
        fut = self._pending.pop(str(frame.get("id")), None)
        if fut is None or fut.done():
            return
        if frame.get("ok") is True:
            fut.set_result({"result": frame.get("result")})
        else:
            err = frame.get("error") or {}
            fut.set_result({"_error_": {"code": str(err.get("code")), "message": str(err.get("message"))}})

    def _schedule_host_request(self, req_id: str, op: str, params: dict) -> None:
        """入站 host 请求 → on_host_request → respond_host（B3）。"""

        async def _run() -> None:
            try:
                if self._on_host_request is None:
                    result: dict = {
                        "_host_error_": {"code": "unknown_op", "message": f"no on_host_request handler for {op}"}
                    }
                else:
                    result = await self._on_host_request(op, params)
                await self.respond_host(req_id, result)
            except Exception as exc:  # noqa: BLE001 — handler 崩溃也必须回帧
                await self.respond_host(req_id, {"_host_error_": {"code": "internal", "message": str(exc)}})

        asyncio.get_running_loop().create_task(_run())

    async def respond_host(self, req_id: str, result: dict) -> None:
        """把 host 请求结果回写给 sidecar（B3）。

        result 含 "_host_error_" → 回 ok:false 错误帧；否则 ok:true + result。
        """
        if self._proc is None or self._proc.stdin is None or self._stopping:
            return
        err = result.get("_host_error_")
        if err is not None:
            frame: dict = {
                "v": 1,
                "id": req_id,
                "ok": False,
                "error": {"code": str(err.get("code")), "message": str(err.get("message"))},
            }
        else:
            frame = {"v": 1, "id": req_id, "ok": True, "result": result}
        try:
            self._proc.stdin.write(encode_frame(frame).encode("utf-8"))
            await self._proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError):
            logger.warning("respond_host %s lost: sidecar pipe closed", req_id)

    # ---------- restart ----------

    async def _handle_process_loss(self, cause: SidecarError) -> None:
        if self._stopping:
            return
        self._ready = False
        self._reject_all(SidecarError("sidecar_restarted", f"{cause}; in-flight request not replayed"))
        if self._proc is not None and self._proc.stdin is not None:
            self._proc.stdin.close()  # 断开写管道，transport 及时收尾（避免 loop 关闭后 GC unraisable）
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stderr_task = None
        if self._restarts_used >= self._max_restarts:
            self._mark_unavailable(cause)
            return
        self._restarts_used += 1
        logger.warning(
            "sidecar crashed (%s); restarting (attempt %d/%d)", cause, self._restarts_used, self._max_restarts
        )
        try:
            await self._spawn()
        except SidecarError:
            return  # _spawn 已 mark_unavailable
        # 重开活跃会话（design：restart 后 reopen JSONL session）
        for session_id in list(self._active_sessions):
            try:
                await self.request("open_session", {"session_id": session_id}, timeout=READY_TIMEOUT_S)
            except SidecarError as exc:
                logger.error("reopen session %s after restart failed: %s", session_id, exc)

    def _mark_unavailable(self, cause: Exception) -> None:
        self._unavailable = True
        self._ready = False
        self._reject_all(SidecarError("pi_sidecar_unavailable", f"sidecar unavailable: {cause}"))
