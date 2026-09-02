#!/usr/bin/env bash
# scripts/desktop/assemble.sh
# 组装桌面打包资源到 .desktop-build/(供 tauri resources 引用)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD="$ROOT/.desktop-build"
RUNTIME="$BUILD/python-runtime"

# 1) 前端构建(复用现有 npm run build,不改前端)
echo "=== Building frontend ==="
( cd "$ROOT/frontend" && npm ci && npm run build )

# 1.1) 前端产物入台到 .desktop-build/frontend/dist
# dev 模式(cargo tauri dev)下 resources.rs 从这里取 res.frontend_dist,
# runtime_dir::prepare 每次启动都把它复制到 runtime 目录给 Python sidecar serve。
# release 由 tauri.conf.json bundle.resources 直接打包仓库根 frontend/dist,不走这里。
# 漏掉这一步 → .desktop-build/frontend/dist 不存在 → runtime_dir::prepare 跳过复制
# → Python 永远 serve runtime 里的旧 dist,dev 前端不更新。
echo "=== Staging frontend/dist to .desktop-build/ ==="
rm -rf "$BUILD/frontend"
mkdir -p "$BUILD/frontend/dist"
cp -R "$ROOT/frontend/dist/." "$BUILD/frontend/dist/"

# 2) 运行时须已由 fetch-runtime.sh + install-deps.sh 准备好
echo "=== Checking runtime ==="
[ -x "$RUNTIME/bin/python3" ] || { echo "runtime missing; run fetch-runtime.sh + install-deps.sh first"; exit 1; }

# 3) 裁剪运行时 site-packages 体积(测试 / 缓存 / dist-info 元数据)
echo "=== Trimming runtime ==="
find "$RUNTIME" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$RUNTIME" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "$RUNTIME" -type d -name "test" -prune -exec rm -rf {} + 2>/dev/null || true
# 不删除 *.dist-info (pip/uv 需要它们来管理包)

# 守卫：确认 trim 后仍保留 .dist-info（pip 需要 metadata 管理 --target 安装）
if ! find "$RUNTIME" -type d -name "*.dist-info" | grep -q .; then
  echo "WARNING: no *.dist-info found in runtime — pip --target install will still work"
  echo "         (it writes new dist-info into ~/.vibe-trading/runtime/libs), but"
  echo "         uninstall/upgrade of bundled core deps would lose metadata."
fi

# 4) 准备 agent 代码模板:复制后删除数据目录,保证 bundle 模板永不含用户数据
echo "=== Preparing agent template ==="
rm -rf "$BUILD/agent"
mkdir -p "$BUILD/agent"
cp -R "$ROOT/agent/." "$BUILD/agent/"
for d in runs sessions uploads .swarm; do rm -rf "$BUILD/agent/$d"; done
find "$BUILD/agent" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$BUILD/agent/tests"

# 4.1) Pi sidecar 暂存（二进制/extensions/skills 由 scripts/desktop/build-pi.sh 产出）
if [ -x "$ROOT/scripts/desktop/build-pi.sh" ]; then
  echo "=== Staging Pi sidecar ==="
  bash "$ROOT/scripts/desktop/build-pi.sh" "$(uname -m | grep -q arm64 && echo macos-aarch64 || echo macos-x86_64)"
fi

# 5) .env 种子:若仓库 agent/.env 不存在,用 agent/.env.example 兜底(不含真密钥)
echo "=== Preparing .env seed ==="
if [ -f "$ROOT/agent/.env" ]; then cp "$ROOT/agent/.env" "$BUILD/agent/.env";
elif [ -f "$ROOT/agent/.env.example" ]; then cp "$ROOT/agent/.env.example" "$BUILD/agent/.env";
else : > "$BUILD/agent/.env"; fi

# 6) VERSION 标记
echo "=== Creating VERSION marker ==="
if [ -n "${DESKTOP_RELEASE_VERSION:-}" ]; then
  VERSION_MARKER="$DESKTOP_RELEASE_VERSION"
else
  # Development builds use a unique marker so runtime_dir::prepare() refreshes
  # bundled frontend/resources even when the installed semantic version matches.
  VERSION_MARKER="$(cd "$ROOT" && git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
fi
printf '%s\n' "$VERSION_MARKER" > "$BUILD/VERSION"
echo "VERSION → $VERSION_MARKER"

echo "=== Assembly complete (Tier 0 runtime + agent source + frontend/dist + .env seed) ==="
echo "Contents of $BUILD:"
du -sh "$BUILD"/*
echo ""
echo "=== Bundle size recording ==="
echo "Tier 0 site-packages size:"
du -sh "$RUNTIME/lib"/*/site-packages 2>/dev/null || du -sh "$RUNTIME/Lib"/*/site-packages 2>/dev/null || echo "(site-packages not found at expected paths)"
echo "Total runtime size:"
du -sh "$RUNTIME"
