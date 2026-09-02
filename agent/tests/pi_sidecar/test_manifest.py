"""manifest 转换单测：BaseTool 属性 → ManifestTool dict。"""

from typing import Any

from src.agent.tools import BaseTool, ToolRegistry
from src.pi_sidecar.manifest import build_manifest_tool, build_tool_manifest

TOOL_TIMEOUT_SECONDS = 1800.0


class ReadOnlyTool(BaseTool):
    name = "web_search"
    description = "Search the web"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    repeatable = True
    is_readonly = True
    side_effecting = False

    def execute(self, **kwargs: Any) -> str:
        return "{}"


class WriteTool(BaseTool):
    name = "trading_place_order"
    description = "Place an order"
    parameters = {"type": "object", "properties": {"symbol": {"type": "string"}}}
    is_readonly = False
    side_effecting = True

    def execute(self, **kwargs: Any) -> str:
        return "{}"


class BashTool(BaseTool):
    name = "bash"
    description = "Run shell"
    is_readonly = True  # 恶意/错误的标注：gateway 按 name 强制 side-effecting
    side_effecting = False

    def execute(self, **kwargs: Any) -> str:
        return "{}"


def _registry(*tools: BaseTool) -> ToolRegistry:
    r = ToolRegistry()
    for t in tools:
        r.register(t)
    return r


class TestBuildManifestTool:
    def test_readonly_tool_fields(self):
        m = build_manifest_tool(ReadOnlyTool())
        assert m == {
            "name": "web_search",
            "description": "Search the web",
            "parameters": ReadOnlyTool.parameters,
            "is_readonly": True,
            "side_effecting": False,
            "repeatable": True,
            "timeout_seconds": TOOL_TIMEOUT_SECONDS,
        }

    def test_side_effecting_flag_true_for_write(self):
        assert build_manifest_tool(WriteTool())["side_effecting"] is True

    def test_bash_is_forced_side_effecting(self):
        # recon §3.3：gateway 对 "bash" 名单强制 side-effecting；manifest 必须一致
        m = build_manifest_tool(BashTool())
        assert m["side_effecting"] is True
        assert m["is_readonly"] is False

    def test_empty_parameters_get_object_schema(self):
        m = build_manifest_tool(BashTool())
        assert m["parameters"] == {"type": "object", "properties": {}, "required": []}


class TestBuildToolManifest:
    def test_all_registry_tools(self):
        manifest = build_tool_manifest(_registry(ReadOnlyTool(), WriteTool(), BashTool()))
        assert [t["name"] for t in manifest] == ["web_search", "trading_place_order", "bash"]

    def test_trading_prefix_forced_side_effecting(self):
        manifest = build_tool_manifest(_registry(WriteTool()))
        assert manifest[0]["side_effecting"] is True
