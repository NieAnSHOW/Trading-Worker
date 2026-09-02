"""Pi sidecar 执行层（engine 开关 + 协议/客户端/桥接子模块）。"""

from __future__ import annotations

import os

_VALID_ENGINES = ("pi", "legacy")

#: Vibe 专有 provider，无法映射到 Pi ModelRegistry（cookie/自定义路径），
#: 选中它们的会话由 SessionService 显式回退 legacy 引擎（design resolved ambiguity）。
LEGACY_ONLY_PROVIDERS = frozenset({"vip_server", "openai-codex"})


def get_agent_engine() -> str:
    """返回执行引擎选择；env VIBE_AGENT_ENGINE ∈ {"pi","legacy"}，默认 "pi"。

    设计约束（design §Idempotency）：legacy 仅作为显式运维回退，默认禁用——
    非法 env 值一律落回 "pi"，绝不静默选 legacy。
    """
    raw = (os.getenv("VIBE_AGENT_ENGINE") or "").strip().lower()
    return raw if raw in _VALID_ENGINES else "pi"
