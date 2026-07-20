#!/usr/bin/env python3
"""Tier 0 冒烟:验证 serve 入口链路顶层可导入且不因缺重型包而崩溃。

与 smoke_imports.py(Tier 1,验证重型包)互补——本脚本在 bundle 装配后跑,
用 bundle 的 Tier 0 运行时执行,任意 ImportError 即非零退出。

ponytail: 覆盖 serve 入口链路顶层模块 + cli/api_server import 可达性,
         重型包不在检查范围(Tier 1 冒烟由 smoke_imports.py 覆盖)。
"""
import sys

# serve 入口链路顶层:cli.main → serve_main 的 import 面。缺任一即 Tier 0 边界判断错。
MODULES = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "langchain",
    "langgraph",
    "sse_starlette",
    "httpx",
    "rich",
    "yaml",
    "dotenv",
    "requests",
    "starlette",
    "websockets",
]


def main() -> int:
    failed: list[tuple[str, str]] = []
    for name in MODULES:
        try:
            __import__(name)
            print(f"OK   import {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, repr(exc)))
            print(f"FAIL import {name}: {exc!r}")

    # 关键:import cli + api_server.app 构造 serve app,不监听端口——
    # 证明入口链路顶层不 import 重型包。
    try:
        import cli  # noqa: F401
        from api_server import app
        print("OK   import cli + api_server.app (serve 入口链路顶层就绪)")
        try:
            snapshot_path = str(app.url_path_for("get_snapshot"))
        except Exception:  # noqa: BLE001 - missing routes must fail the smoke, not crash it
            snapshot_path = None
        if snapshot_path == "/news-api/snapshot":
            print("OK   route /news-api/snapshot (news API 已注册)")
        else:
            failed.append(("/news-api/snapshot", "route is not registered"))
            print("FAIL route /news-api/snapshot: route is not registered")
    except Exception as exc:  # noqa: BLE001
        failed.append(("cli/api_server", repr(exc)))
        print(f"FAIL import cli/api_server: {exc!r}")

    if failed:
        print(f"\nTIER0 SMOKE FAILED: {len(failed)} issue(s)")
        return 1
    print("\nTIER0 SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
