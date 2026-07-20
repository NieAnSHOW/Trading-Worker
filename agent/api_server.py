#!/usr/bin/env python3
"""Vibe-Trading API Server - RESTful API for finance research and backtesting.

Thin assembler: creates the FastAPI app, mounts middleware, registers route
modules, and re-exports symbols for test compatibility.  All shared
infrastructure lives in ``src.api.{security,models,helpers,state}``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status  # noqa: F401
from fastapi.responses import FileResponse, JSONResponse  # noqa: F401
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from rich.console import Console

from cli._version import __version__ as APP_VERSION
from src.ui_services import build_run_analysis, load_run_context  # noqa: F401

# UTF-8 on Windows
import sys as _sys
for _s in ("stdout", "stderr"):
    _r = getattr(getattr(_sys, _s, None), "reconfigure", None)
    if callable(_r):
        _r(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Extracted infrastructure — re-exported for route-module and test access
# ---------------------------------------------------------------------------

from src.api.security import (  # noqa: F401, E402
    _API_KEY,
    _CORS_ORIGINS,
    _DEFAULT_CORS_ORIGINS,
    _DEFAULT_LOOPBACK_HOSTS,
    _DOCKER_LOOPBACK_ENV,
    _EXTRA_LOOPBACK_HOSTS,
    _SAFE_BROWSER_METHODS,
    _SHELL_TOOLS_ENV,
    _UvicornQuerySecretRedactionFilter,
    _add_security_response_headers,
    _auth_credential_from_header_or_query,
    _configured_api_key,
    _default_gateway_ips,
    _env_flag_enabled,
    _env_shell_tools_enabled,
    _host_without_port,
    _is_allowed_loopback_host,
    _is_local_client,
    _is_loopback_bind_host,
    _is_loopback_origin,
    _origin_matches_request_host,
    _parse_cors_origins,
    _parse_extra_loopback_hosts,
    _reject_cross_site_browser_request,
    _reject_untrusted_browser_write_middleware,
    _reject_untrusted_loopback_host,
    _require_shutdown_authorization,
    _mint_sse_ticket,
    _security,
    _shell_tools_enabled_for_request,
    _trusted_docker_loopback_ip,
    _validate_api_auth,
    require_auth,
    require_event_stream_auth,
    require_local_or_auth,
    require_settings_write_auth,
)

from src.api.models import (  # noqa: F401, E402
    Artifact,
    BacktestMetrics,
    RAGSelection,
    RunInfo,
    RunResponse,
)

from src.api.helpers import (  # noqa: F401, E402
    AGENT_DIR,
    ENV_EXAMPLE_PATH,
    ENV_PATH,
    RUNS_DIR,
    SESSIONS_DIR,
    UPLOADS_DIR,
    _coerce_float,
    _coerce_int,
    _ensure_agent_env_file,
    _format_env_value,
    _FRONTEND_DIST,
    _is_configured_secret,
    _is_spa_html_route,
    _project_relative_path,
    _read_env_values,
    _SAFE_PATH_PARAM_RE,
    _spa_html_deep_link_fallback,
    _strip_env_value,
    _validate_path_param,
    _write_env_values,
)

from src.api.state import (  # noqa: F401, E402
    _channel_bus,
    _channel_manager,
    _channel_runtime,
    _get_channel_runtime,
    _get_session_service,
    _session_service,
)

# ---------------------------------------------------------------------------
# fork: desktop runtime env-path resolution
# ---------------------------------------------------------------------------
# Packaged desktop runs from ~/.vibe-trading/runtime/agent/, and the user's
# settings must persist to ~/.vibe-trading/.env (not the read-only bundled
# agent/.env). _resolve_settings_env_path picks the right dotenv based on
# whether AGENT_DIR sits under the user runtime dir. settings_routes reads
# host.ENV_PATH (late-binding via sys.modules), so overriding the module
# attribute here is enough — no edit to settings_routes needed.
USER_ENV_PATH = Path.home() / ".vibe-trading" / ".env"


def _is_runtime_agent(agent_dir: "Path | None" = None, user_env_path: "Path | None" = None) -> bool:
    """Return True when the API server is running from the desktop runtime copy."""
    resolved_agent_dir = agent_dir or AGENT_DIR
    resolved_user_env_path = user_env_path or USER_ENV_PATH
    return resolved_agent_dir.parent.parent == resolved_user_env_path.parent


def _resolve_settings_env_path(
    agent_env_path: "Path | None" = None,
    user_env_path: "Path | None" = None,
) -> Path:
    """Return the dotenv file that should back Settings reads and writes."""
    resolved_agent_env_path = agent_env_path or ENV_PATH
    resolved_user_env_path = user_env_path or USER_ENV_PATH
    if _is_runtime_agent(resolved_agent_env_path.parent, resolved_user_env_path):
        return resolved_user_env_path
    return resolved_agent_env_path


# Override the helpers-module default so settings routes write to the user env
# when running as the desktop sidecar.
ENV_PATH = _resolve_settings_env_path()

console = Console()
logger = logging.getLogger(__name__)


def _accept_header(scope: Dict[str, Any]) -> str:
    """Read the request Accept header from an ASGI scope."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"accept":
            return value.decode("latin-1")
    return ""


class SPAStaticFiles(StaticFiles):
    """Serve browser SPA paths without turning API 404s into HTML."""

    async def get_response(self, path: str, scope: Dict[str, Any]):
        from starlette.exceptions import HTTPException as StarletteHTTPException

        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            is_news_api = path.strip("/").startswith("news-api/") or path.strip("/") == "news-api"
            if is_news_api or "text/html" not in _accept_header(scope):
                return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})
            return await super().get_response("index.html", scope)

# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="Vibe-Trading API",
    description="Vibe-Trading API: natural-language finance research, backtesting, and swarm workflows",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware functions are defined in src.api.security / src.api.helpers, so
# the @app.middleware("http") decorator cannot be used here — register them
# programmatically instead.
app.middleware("http")(_reject_untrusted_loopback_host)
app.middleware("http")(_reject_untrusted_browser_write_middleware)
app.middleware("http")(_spa_html_deep_link_fallback)
app.middleware("http")(_add_security_response_headers)


# ponytail: best-effort global error telemetry — must never affect requests
@app.middleware("http")
async def _telemetry_error_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        try:
            from starlette.exceptions import HTTPException  # noqa: PLC0415
            if not isinstance(exc, HTTPException):
                from src.telemetry import counters  # noqa: PLC0415
                counters.record_error(type(exc).__name__)
        except Exception:  # noqa: BLE001 - telemetry must never break a request
            pass
        raise

# ============================================================================
# Lifecycle hooks
# ============================================================================

from src.api.channels_routes import (  # noqa: E402
    _start_channel_runtime,
    _stop_channel_runtime,
)
from src.api.scheduled_routes import (  # noqa: E402
    _start_scheduled_research_executor,
    _stop_scheduled_research_executor,
)


@app.on_event("startup")
async def _run_startup_preflight() -> None:
    """Run preflight checks on server startup."""
    from src.preflight import run_preflight

    run_preflight(console)
    _start_scheduled_research_executor()
    await _watchlist_init_db()  # 幂等建表；plan 约束：DB 初始化须在 startup，不在首次请求中建
    if os.getenv("VIBE_TRADING_CHANNELS_AUTO_START", "").strip().lower() in {"1", "true", "yes"}:
        await _start_channel_runtime()


@app.on_event("shutdown")
async def _stop_scheduled_research_on_shutdown() -> None:
    """Stop the scheduled research executor on server shutdown."""
    await _stop_channel_runtime()
    await _stop_scheduled_research_executor()


async def _close_news_coordinator() -> None:
    """Release the same lazy coordinator instance used by the news routes."""
    await news_coordinator.close()


app.on_event("shutdown")(_close_news_coordinator)


# ============================================================================
# Route registration + re-exports
# ============================================================================

# --- Runs ---
from src.api.runs_routes import register_runs_routes  # noqa: E402
register_runs_routes(app)

from src.api.runs_routes import (  # noqa: F401, E402
    _load_json_file,
    _load_csv_to_dict,
    _build_response_from_run_dir,
)

# --- Sessions ---
from src.api.sessions_routes import register_sessions_routes  # noqa: E402
register_sessions_routes(app)

from src.api.sessions_routes import (  # noqa: F401, E402
    _goal_store,
    _live_action_frame_from_tool_result,
    _mandate_proposal_frame_from_tool_result,
)

# --- System ---
from src.api.system_routes import register_system_routes  # noqa: E402
register_system_routes(app)

from src.api.system_routes import _terminate_current_process  # noqa: F401, E402

# --- Browser EventSource authentication ---
from src.api.auth_routes import router as auth_router  # noqa: E402
app.include_router(auth_router)

# --- Settings ---
from src.api.settings_routes import register_settings_routes  # noqa: E402
register_settings_routes(app)

from src.api.settings_routes import (  # noqa: F401, E402
    _baostock_supported,
    _baostock_installed,
    _load_llm_providers,
    UpdateLLMSettingsRequest,
    LLMSettingsResponse,
)

# --- Uploads ---
from src.api.uploads_routes import register_uploads_routes  # noqa: E402
register_uploads_routes(app)

from src.api.uploads_routes import (  # noqa: F401, E402
    MAX_UPLOAD_SIZE,
    _BLOCKED_UPLOAD_EXT,
    _BLOCKED_UPLOAD_NAMES,
    _SHADOW_ID_RE,
    _UPLOAD_CHUNK_SIZE,
)

# --- Channels ---
from src.api.channels_routes import register_channels_routes  # noqa: E402
register_channels_routes(app)
from src.api.qveris_routes import qveris_router  # noqa: E402  # QVERIS-INTEGRATION
app.include_router(qveris_router)  # QVERIS-INTEGRATION

from src.api.channels_routes import (  # noqa: F401, E402
    ChannelPairingCommandRequest,
)

# --- Swarm ---
from src.api.swarm_routes import register_swarm_routes  # noqa: E402
register_swarm_routes(app)

from src.api.swarm_routes import _get_swarm_runtime  # noqa: F401, E402

# --- Live trading ---
from src.api.live_routes import register_live_routes  # noqa: E402
register_live_routes(app)

from src.api.live_routes import (  # noqa: F401, E402
    CommitMandateRequest,
    LiveHaltRequest,
    LiveAuthorizeRequest,
    LiveRunnerControlRequest,
    BrokerAuthState,
    MandateLimits,
    ActiveMandateState,
    RunnerLivenessState,
    LiveBrokerStatus,
    LiveStatusResponse,
    LiveRunnerUnavailable,
    _runner_tasks,
    _runner_factory,
    _emit_live_event,
    _fetch_broker_ceilings,
    _known_live_brokers,
    _oauth_token_present,
    _active_mandate_state,
    _runner_liveness_state,
    _live_broker_adapter,
    _build_live_runner,
    _drive_runner,
)

# --- Alpha Zoo ---
from src.api.alpha_routes import register_alpha_routes  # noqa: E402
register_alpha_routes(app)

# --- Watchlist ---
from src.api.watchlist_routes import register_watchlist_routes, init_db as _watchlist_init_db  # noqa: E402
register_watchlist_routes(app)

# --- Dashboard ---
from src.api.dashboard_routes import register_dashboard_routes  # noqa: E402
register_dashboard_routes(app, require_auth=require_auth)

# --- Investment news ---
# Register before ``serve_main`` can mount the root SPA catch-all.
from src.api.news_routes import register_news_routes  # noqa: E402
from src.news.coordinator import get_news_coordinator  # noqa: E402

news_coordinator = get_news_coordinator()
register_news_routes(app, require_auth=require_auth, coordinator=news_coordinator)


# ============================================================================
# Scheduled Research Routes - defined in src/api/scheduled_routes.py
# ============================================================================
#
# Lightweight CRUD endpoints backed by ScheduledResearchJobStore. The endpoint
# handlers only record and expose jobs; the optional executor lifecycle is
# guarded separately by VIBE_TRADING_ENABLE_SCHEDULER.

from src.api.scheduled_routes import register_scheduled_routes  # noqa: E402
register_scheduled_routes(app)

from src.api.scheduled_routes import (  # noqa: E402, F401
    CreateScheduledRunRequest,
    ScheduledRunResponse,
    _dispatch_scheduled_research_job,
    _get_scheduled_research_executor,
    _get_scheduled_research_store,
    _scheduled_research_scheduler_enabled,
)


# ============================================================================
# Fork-only routes — desktop sidecar additions (no upstream equivalent)
# ============================================================================

# --- Optional deps — on-demand broker SDK install (desktop runtime) ---
# Mounted with the same loopback-or-auth gate as the other settings endpoints
# so a non-local client must present API_AUTH_KEY to install packages.
from src.optional_deps.api import router as optional_deps_router  # noqa: E402

app.include_router(
    optional_deps_router,
    dependencies=[Depends(require_local_or_auth)],
)


# --- Telemetry — local same-origin aggregated metrics (§6.1) ---
@app.get("/telemetry/sidecar-metrics")
async def telemetry_sidecar_metrics(since: float | None = None):
    """本地同源聚合指标。无鉴权，仅回环访问。

    返回自上次 snapshot 以来的增量并重置窗口；响应仅聚合数字。
    """
    from src.telemetry import counters  # 局部 import，避免启动期循环依赖
    return counters.snapshot(since)


# --- WeChat QR login — in-page scan for the weixin IM channel ---
async def _weixin_connectivity_probe(base_url: str) -> dict:
    """在 sidecar 进程内诊断到 base_url 的连通性(ConnectError 定位用)。

    begin_qr_login/poll_qr_login 抛 httpx 异常时调用,在同一进程内重新解析 DNS
    并尝试连接,把结果写进 detail —— 无需用户手动跑脚本,就能在报错时刻拿到
    sidecar 进程看到的真实网络状态(目标域名、DNS 是否 fake-ip、直连能否连通、
    是否有继承的代理环境变量),一次性区分 DNS 缓存/代理/进程网络/base_url 错误。
    """
    import socket as _socket
    from urllib.parse import urlparse

    out: dict = {"base_url": base_url}
    proxy_env = {
        k: os.environ[k]
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                  "http_proxy", "https_proxy", "all_proxy", "no_proxy")
        if os.environ.get(k)
    }
    out["proxy_env"] = proxy_env or "(none)"
    host = urlparse(base_url).hostname or base_url
    try:
        infos = _socket.getaddrinfo(host, 443)
        out["dns"] = sorted({i[4][0] for i in infos})
    except Exception as e:  # noqa: BLE001
        out["dns_err"] = f"{type(e).__name__}: {e}"
    for label, trust in (("direct", False), ("default_env", True)):
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=trust) as c:
                r = await c.get(base_url, headers={"User-Agent": "vibe-probe"})
                out[f"probe_{label}"] = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            cause = e.__cause__ or e.__context__
            msg = f"{type(e).__name__}: {e}"
            if cause and cause is not e:
                msg += f" [cause: {type(cause).__name__}: {cause}]"
            out[f"probe_{label}"] = "FAIL " + msg
    return out


@app.post("/channels/weixin/login/start", dependencies=[Depends(require_auth)])
async def weixin_login_start():
    """Begin WeChat QR login; returns {login_id, qr_image} for in-page scan."""
    runtime = _get_channel_runtime()
    manager = runtime.manager
    if manager is None:
        raise HTTPException(status_code=400, detail="weixin channel unavailable; enable it first")
    adapter = manager.get_channel("weixin")
    if adapter is None:
        raise HTTPException(status_code=400, detail="weixin channel unavailable; enable it first")
    try:
        return await adapter.begin_qr_login()
    except httpx.HTTPError as exc:
        probe = await _weixin_connectivity_probe(adapter.config.base_url)
        logger.exception(
            "WeChat QR login request failed to %s; in-process probe=%s",
            adapter.config.base_url, probe,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"微信登录连接失败 {type(exc).__name__}: {exc} | 进程内诊断: {probe}",
        )
    except Exception as exc:  # noqa: BLE001 - surface adapter failure with a readable cause
        logger.exception("WeChat QR login failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"微信登录失败: {type(exc).__name__}: {exc}",
        )


@app.get("/channels/weixin/login/status", dependencies=[Depends(require_auth)])
async def weixin_login_status(login_id: str = Query(...)):
    """Poll WeChat QR login status for the given login_id."""
    runtime = _get_channel_runtime()
    manager = runtime.manager
    if manager is None:
        raise HTTPException(status_code=400, detail="weixin channel unavailable")
    adapter = manager.get_channel("weixin")
    if adapter is None:
        raise HTTPException(status_code=400, detail="weixin channel unavailable")
    try:
        result = await adapter.poll_qr_login(login_id)
        # 扫码换新 bot 后,正在运行的 poll 循环仍绑在旧 token 上。此处(协调层,
        # 在 REST 请求上下文而非 poll 循环 task 内)驱动 restart,避免 task 自
        # cancel 竞态。用 adapter 的 _pending_reconnect 标志保证幂等:前端每 2s
        # 轮询会多次拿到 confirmed,但只在首次 token 变化时重启一次。
        if result.get("status") == "confirmed" and getattr(adapter, "_pending_reconnect", False):
            adapter._pending_reconnect = False
            # 若运行时尚未启动(用户先扫码、从未点 Start),先整体启动;
            # 否则只重启 weixin 单通道以加载新 token。
            if not runtime._running:
                await runtime.start(start_manager=True)
            else:
                await manager.restart_channel("weixin")
            result["reconnected"] = True
        return result
    except httpx.HTTPError as exc:
        probe = await _weixin_connectivity_probe(adapter.config.base_url)
        logger.exception(
            "WeChat QR login status poll failed to %s; in-process probe=%s",
            adapter.config.base_url, probe,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"微信登录状态查询失败 {type(exc).__name__}: {exc} | 进程内诊断: {probe}",
        )
    except Exception as exc:  # noqa: BLE001 - surface adapter failure with a readable cause
        logger.exception("WeChat QR login status poll failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"微信登录状态查询失败: {type(exc).__name__}: {exc}",
        )


# ============================================================================
# Main Entry Point
# ============================================================================

def _has_root_route(routes: Any) -> bool:
    """Return whether a FastAPI/Starlette route collection already owns ``/``."""
    return any(getattr(route, "path", None) == "/" for route in routes)


class _NonSuccessAccessFilter(logging.Filter):
    """只让非成功 (>=400) 的访问请求写进 access log。

    uvicorn access 的 LogRecord.args 为
    (client_addr, method, path, http_version, status_code),其中 status_code
    是唯一的 int,取它判断即可 (3.11/3.12 结构已核实一致)。这样前端常驻轮询的
    200 健康检查 (/live/status、/channels/status) 不再刷日志,4xx/5xx 照常记录。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for arg in record.args or ():
            if isinstance(arg, int):  # status_code
                return arg >= 400
        return True  # 解析不到状态码则照常记 (偏保守)


def _build_access_log_config() -> dict:
    """拷贝 uvicorn 默认 LOGGING_CONFIG,给 uvicorn.access 挂上非成功过滤。

    必须走 log_config:uvicorn.run 内部会 dictConfig 重置 logger,
    在 run 之前手动 addFilter 会被覆盖。
    """
    import copy

    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    filters = cfg.setdefault("filters", {})
    filters["nonsuccess"] = {"()": _NonSuccessAccessFilter}
    filters["redact_query_secrets"] = {"()": _UvicornQuerySecretRedactionFilter}
    cfg["loggers"]["uvicorn.access"]["filters"] = ["redact_query_secrets", "nonsuccess"]
    return cfg


def serve_main(argv: list[str] | None = None) -> int:
    """Start the API server from CLI-style arguments."""
    import argparse
    import subprocess
    import uvicorn
    parser = argparse.ArgumentParser(description="Vibe-Trading Server")
    parser.add_argument("--port", type=int, default=8000, help="Listen port (default 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--dev", action="store_true", help="Dev mode: spawn Vite on :5173")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if not _is_loopback_bind_host(args.host) and not _configured_api_key():
        print(
            f"[warn] Binding to {args.host} without API_AUTH_KEY set. "
            f"Remote requests are rejected by the loopback peer-IP check, "
            f"but consider using --host 127.0.0.1 for local-only access."
        )

    frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    frontend_root = Path(__file__).resolve().parent.parent / "frontend"

    vite_proc = None
    if args.dev and frontend_root.exists():
        print("[dev] Starting Vite dev server on :5173 ...")
        vite_proc = subprocess.Popen(
            ["npx", "vite", "--host", "0.0.0.0"],
            cwd=str(frontend_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[dev] Vite PID={vite_proc.pid}")
        print("[dev] Frontend: http://localhost:5173")
        print(f"[dev] API: http://localhost:{args.port}")
    elif frontend_dist.exists():
        if not _has_root_route(app.routes):
            app.mount("/", SPAStaticFiles(directory=str(frontend_dist), html=True), name="frontend")
        print(f"[prod] Frontend served from {frontend_dist}")
    else:
        print(f"[warn] No frontend build found at {frontend_dist}")
        print("[warn] Run: cd frontend && npm run build")

    print("=" * 50)
    print("  Vibe-Trading Server")
    print(f"  http://127.0.0.1:{args.port}")
    print("=" * 50)

    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
            log_config=_build_access_log_config(),
        )
    finally:
        if vite_proc:
            vite_proc.terminate()
            print("[dev] Vite stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_main())
