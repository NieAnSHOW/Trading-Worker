#!/usr/bin/env bash
# Build + stage the Pi sidecar binary for one release target.
# Usage: build-pi.sh <macos-aarch64|macos-x86_64|windows-x64>
# Prereq: Bun >= 1.3.14; Node >= 22.19 not required for compile (Bun compiles) but CI pins it for other steps.
set -euo pipefail

TARGET="${1:?usage: build-pi.sh <macos-aarch64|macos-x86_64|windows-x64>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PI_DIR="$ROOT/pi-sidecar"
OUT="$ROOT/.desktop-build/pi"

case "$TARGET" in
  macos-aarch64) BUN_TARGET="bun-darwin-arm64"; BIN="pi";    PLAT="darwin-arm64" ;;
  macos-x86_64)  BUN_TARGET="bun-darwin-x64";  BIN="pi";     PLAT="darwin-x64" ;;
  windows-x64)   BUN_TARGET="bun-windows-x64"; BIN="pi.exe"; PLAT="win32-x64" ;;
  *) echo "unknown target: $TARGET" >&2; exit 2 ;;
esac

cd "$PI_DIR"
bun install --frozen-lockfile   # 精确锁定（design §Packaging：lockfile 提交）

# 发布闸门：内建工具封锁（write/edit/bash 不绕过 Python ToolGateway）是行为性
# 保证 —— toolNames:[] + plan.enabled:false 依赖 SDK 内部 ensureWriteRegistered 等
# 注册行为，非 API 级白名单（restrictToolNames:true 会断掉 extension 加载，见
# task-16 报告）。编译前跑完整 sidecar 测试套件（含 session-driver.test.ts 的
# 内建工具不在场断言）：SDK 行为漂移在此失败并中止构建，而不是静默进入发布包。
bun test
# omp-legacy-pi-modules: SDK 私有编译期虚拟模块（npm 包不含），仅在编译态 legacy
# 扩展加载路径用到；sidecar 只用 createAgentSession + 现代扩展，标记 external 保持
# 未解析即可（与 SDK 自身 bundle-dist.ts 的 ALWAYS_EXTERNAL 一致；ping 冒烟见步骤 10）。
# --outdir（非 --outfile）：SDK 根导出图内含模板/文档资产，会产出多文件。
rm -rf dist
bun build src/main.ts --outdir dist --target bun --external omp-legacy-pi-modules
mkdir -p "$OUT/extensions" "dist/extensions"
# extension 产物同时：(a) 暂存到 $OUT/extensions/（tauri 资源契约）；
# (b) 嵌入编译二进制 $bunfs/root/extensions/ —— main.ts 以 `${import.meta.dir}/extensions/*.js`
#     动态 import，编译态 import.meta.dir = /$bunfs/root，只有嵌入才能命中。
for ext in vibe-memory vibe-providers; do
  bun build "extensions/$ext/index.ts" --outfile "$OUT/extensions/$ext.js" --target bun
  cp "$OUT/extensions/$ext.js" "dist/extensions/$ext.js"
done
mkdir -p "$OUT/$TARGET"
bun build --compile dist/main.js dist/extensions/vibe-providers.js dist/extensions/vibe-memory.js \
  --target "$BUN_TARGET" --external omp-legacy-pi-modules --outfile "$OUT/$TARGET/$BIN"

# N-API 原生插件：pi-natives（SDK npm 包 embeddedAddon=null，编译态二进制从 execDir
# 候选路径加载 pi_natives.<plat>.node）；从 per-platform leaf 包拷贝到二进制旁。
# 跨目标编译（如 arm64 主机编 macos-x86_64）需另行安装匹配的 leaf 包。
NATIVE_SRC="$PI_DIR/node_modules/@oh-my-pi/pi-natives-$PLAT/pi_natives.$PLAT.node"
if [ -f "$NATIVE_SRC" ]; then
  cp "$NATIVE_SRC" "$OUT/$TARGET/"
else
  echo "warning: $NATIVE_SRC not found — 编译态二进制将无法加载 pi_natives（需安装匹配的 @oh-my-pi/pi-natives-$PLAT leaf 包）" >&2
fi

# skills 暂存（86 个 SKILL.md 目录；与 agent 资源中的 src/skills 同源）
rm -rf "$OUT/skills"
mkdir -p "$OUT/skills"
cp -R "$ROOT/agent/src/skills/." "$OUT/skills/"

# provenance（design：版本/lockfile/provenance 随桌面构建记录）
PI_VERSION="18.0.11"
LOCK_HASH=$(shasum -a 256 "$PI_DIR/bun.lock" | cut -d' ' -f1)
cat > "$OUT/PROVENANCE.json" <<EOF
{"pi_npm_version": "$PI_VERSION", "bun_lock_sha256": "$LOCK_HASH", "target": "$TARGET", "built_at": "$(date -u +%FT%TZ)"}
EOF

echo "staged $OUT/$TARGET/$BIN"
