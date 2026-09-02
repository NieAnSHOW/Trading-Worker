#!/usr/bin/env bash
# scripts/desktop/build-dmg.sh
#
# 完整构建 macOS .dmg 安装包的端到端脚本。
#
# 作用范围：
#   1. 校验前置工具链（cargo, npm, iconutil, sips）
#   2. 自动准备嵌入式 Python 运行时（缺失则 fetch-runtime + install-deps）+ 校验 assemble 产物（agent/.env/VERSION）
#   3. 构建 console-dist（webview）+ assemble 资源模板（含 frontend/dist）
#   4. 调用 `cargo tauri build` 编译 Rust release + 生成 .app / .dmg
#   5. 冒烟检查：.app 可执行位、icon.icns 大小、.dmg 产物存在
#   6. 输出产物路径与体积摘要
#
# 用法：
#   bash scripts/desktop/build-dmg.sh              # 标准构建（runtime 缺失时自动下载）
#   bash scripts/desktop/build-dmg.sh --no-smoke        # 跳过冒烟检查
#   bash scripts/desktop/build-dmg.sh --skip-runtime    # 跳过 runtime 自动准备（调试已有 runtime）
#
# 前置条件：
#   - runtime/依赖缺失时本脚本自动调用 fetch-runtime.sh + install-deps.sh（--skip-runtime 可禁用）
#   - assemble 产物缺失时本脚本自动调用 assemble.sh 兜底
#   - 固定 PBS 版本：调用前 export PBS_TAG / PBS_ASSET（默认 20260610，asset 按架构自动选）
#
# 退出码：
#   0 成功 / 1 工具缺失 / 2 资源缺失 / 3 构建失败 / 4 冒烟失败 / 5 签名/公证失败

set -euo pipefail

# ── 路径与颜色 ───────────────────────────────────────────────
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_TAURI="$ROOT/src-tauri"
BUILD="$ROOT/.desktop-build"

# 颜色输出（非 TTY 时自动禁用）
if [ -t 1 ]; then
    C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'
    C_YELLOW='\033[33m'; C_RED='\033[31m'; C_BLUE='\033[34m'
else
    C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_BLUE=''
fi

log()  { printf "${C_BLUE}▸${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_GREEN}✓${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}!${C_RESET} %s\n" "$*" >&2; }
err()  { printf "${C_RED}✗${C_RESET} %s\n" "$*" >&2; }
section() { printf "\n${C_BOLD}${C_BLUE}━━━ %s ━━━${C_RESET}\n" "$*"; }

# ── 参数解析 ─────────────────────────────────────────────────
SKIP_SMOKE=0
SKIP_RUNTIME=0
for arg in "$@"; do
    case "$arg" in
        --no-smoke)      SKIP_SMOKE=1 ;;
        --skip-runtime)  SKIP_RUNTIME=1 ;;
        --help|-h)
            sed -n '2,28p' "$0"
            exit 0 ;;
        *) err "未知参数: $arg"; exit 1 ;;
    esac
done

# ── 平台守卫 ─────────────────────────────────────────────────
section "平台检查"
if [ "$(uname)" != "Darwin" ]; then
    err "本脚本仅用于 macOS。Windows 请使用 cargo tauri build 或对应 .ps1 脚本。"
    exit 1
fi
ARCH="$(uname -m)"
ok "macOS $ARCH"

# ── 工具链校验 ───────────────────────────────────────────────
section "工具链校验"
missing=0
check_tool() {
    if command -v "$1" >/dev/null 2>&1; then
        ok "$1 → $(command -v "$1")"
    else
        err "$1 未安装"
        missing=1
    fi
}
check_tool cargo
check_tool npm
check_tool iconutil    # macOS 自带
check_tool sips        # macOS 自带

# tauri CLI（cargo 子命令）
if cargo tauri --version >/dev/null 2>&1; then
    ok "tauri-cli → $(cargo tauri --version 2>&1 | head -1)"
else
    err "tauri-cli 未安装（运行: cargo install tauri-cli --version '^2'）"
    missing=1
fi

[ "$missing" -ne 0 ] && { err "工具链不完整，请先安装缺失项"; exit 1; }

# ── 嵌入式 Python 运行时 ─────────────────────────────────────
PY_RUNTIME="$BUILD/python-runtime/bin/python3"
section "嵌入式 Python 运行时"
# runtime 就绪 = python3 可执行 + Tier 0 依赖可 import。只看可执行位会漏掉
# "fetch 过 runtime 但从未跑 install-deps"的状态，导致 site-packages 只有 pip，
# 一路静默到冒烟检查才爆炸。
if [ -x "$PY_RUNTIME" ] && PYTHONPATH=agent "$PY_RUNTIME" -c "import fastapi" >/dev/null 2>&1; then
    ok "runtime 就绪（含 Tier 0 依赖）→ $("$PY_RUNTIME" --version 2>&1)"
elif [ "$SKIP_RUNTIME" -eq 1 ]; then
    err "runtime 未就绪但指定了 --skip-runtime；移除该参数，或先手动运行 fetch-runtime.sh + install-deps.sh"
    exit 2
else
    if [ ! -x "$PY_RUNTIME" ]; then
        warn "runtime 未就绪，自动调用 fetch-runtime.sh"
        log "bash scripts/desktop/fetch-runtime.sh $BUILD/python-runtime  (PBS_TAG/PBS_ASSET 可选)"
        if ! bash "$ROOT/scripts/desktop/fetch-runtime.sh" "$BUILD/python-runtime"; then
            err "fetch-runtime.sh 失败。如需固定版本，export PBS_TAG / PBS_ASSET 后重试。"
            exit 2
        fi
    else
        warn "runtime 存在但 Tier 0 依赖缺失，自动调用 install-deps.sh"
    fi
    log "bash scripts/desktop/install-deps.sh $BUILD/python-runtime"
    if ! bash "$ROOT/scripts/desktop/install-deps.sh" "$BUILD/python-runtime"; then
        err "install-deps.sh 失败。"
        exit 2
    fi
    [ -x "$PY_RUNTIME" ] || { err "fetch-runtime + install-deps 完成后 runtime 仍不可用"; exit 2; }
    ok "runtime 已就绪 → $("$PY_RUNTIME" --version 2>&1)"
fi

# ── 资源校验 ─────────────────────────────────────────────────
section ".desktop-build 资源检查"
verify_resource() {
    if [ -e "$1" ]; then ok "$2"; else err "缺失: $2 ($1)"; return 1; fi
}

resources_ok=1
# runtime 已由上一个 section 保证，这里只校验 assemble 产物（agent/.env/VERSION/pi）
verify_resource "$BUILD/agent"       "agent 代码模板"    || resources_ok=0
verify_resource "$BUILD/agent/.env"  "agent .env 种子"   || resources_ok=0
verify_resource "$BUILD/VERSION"     "VERSION 标记"      || resources_ok=0
verify_resource "$BUILD/pi"          "pi sidecar 资源"   || resources_ok=0

if [ "$resources_ok" -ne 1 ]; then
    warn "assemble 产物不全，调用 assemble.sh 兜底组装…"
    if [ -x "$ROOT/scripts/desktop/assemble.sh" ]; then
        if ! bash "$ROOT/scripts/desktop/assemble.sh"; then
            err "assemble.sh 失败。"
            exit 2
        fi
        ok "assemble.sh 完成，重新校验资源"
        resources_ok=1
        verify_resource "$BUILD/agent"       "agent 代码模板"    || resources_ok=0
        verify_resource "$BUILD/agent/.env"  "agent .env 种子"   || resources_ok=0
        verify_resource "$BUILD/VERSION"     "VERSION 标记"      || resources_ok=0
        verify_resource "$BUILD/pi"          "pi sidecar 资源"   || resources_ok=0
        [ "$resources_ok" -ne 1 ] && { err "assemble 后仍缺资源"; exit 2; }
    else
        err "找不到 scripts/desktop/assemble.sh，无法兜底"
        exit 2
    fi
fi

section "刷新桌面资源模板"
log "bash scripts/desktop/build-console.sh"
if ! bash "$ROOT/scripts/desktop/build-console.sh"; then
    err "build-console.sh 失败。"
    exit 2
fi

# console-dist 是 Tauri frontendDist（webview 加载的桌面控制台）。build-console.sh
# 已构建；此处校验产出非空，防止空目录被 embed 进 .app 导致 release 启动白屏
# （build-console.sh 退出码只能证明构建命令跑通，不能证明产物有效）。
CONSOLE_DIST_INDEX="$SRC_TAURI/console-dist/index.html"
if [ ! -s "$CONSOLE_DIST_INDEX" ]; then
    err "console-dist 产出缺失或为空: $CONSOLE_DIST_INDEX"
    err "webview 将白屏。请检查 build-console.sh / console-app 源码。"
    exit 2
fi
ok "console-dist/index.html 就绪"

log "bash scripts/desktop/assemble.sh"
if ! bash "$ROOT/scripts/desktop/assemble.sh"; then
    err "assemble.sh 失败。请先确认 python-runtime 已准备好。"
    exit 2
fi
ok "agent / frontend / VERSION 资源模板已刷新"

PY_VER="$("$PY_RUNTIME" --version 2>&1)"
ok "运行时: $PY_VER"

section "嵌入式 Python 冒烟检查 (Tier 0)"
log "PYTHONPATH=agent $PY_RUNTIME scripts/desktop/smoke_tier0.py"
( cd "$ROOT" && PYTHONPATH=agent "$PY_RUNTIME" scripts/desktop/smoke_tier0.py ) || {
    err "嵌入式 Python Tier 0 冒烟检查失败，请重新运行 install-deps.sh"
    exit 2
}
ok "嵌入式 Python Tier 0 冒烟检查通过"

# ── 强制刷新 VERSION 标记 ─────────────────────────────────────
# 每次构建必须生成唯一 VERSION，否则 runtime_dir::prepare() 会因版本
# 匹配而跳过 frontend/dist 刷新（Action::Reuse），导致新版客户端显示旧 UI。
section "刷新 VERSION 标记"
if [ -n "${DESKTOP_RELEASE_VERSION:-}" ]; then
    VERSION_NEW="$DESKTOP_RELEASE_VERSION"
else
    VERSION_NEW="$(cd "$ROOT" && git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
fi
echo "$VERSION_NEW" > "$BUILD/VERSION"
ok "VERSION → $VERSION_NEW"

# ── Tauri 编译 + 打包 ────────────────────────────────────────
section "Tauri build (cargo tauri build)"
log "开始编译 Rust release 并打包 .app / .dmg（首次约 1-3 分钟）"
# ── 编译期注入线上用户接口地址 ─────────────────────────────────
# option_env! 在编译期从构建进程环境取值。手动 export 优先；
# 否则从 gitignored 的 src-tauri/.env 读取（与 CI secret 注入、
# 运行时 dotenvy 优先级一致）。两者都没有时产物回退 127.0.0.1:8001。
if [ -z "${VIBE_USER_API_URL:-}" ] && [ -f "$SRC_TAURI/.env" ]; then
    _url="$(sed -n 's/^VIBE_USER_API_URL=//p' "$SRC_TAURI/.env" | head -1 | tr -d '\r' | tr -d '"' || true)"
    if [ -n "$_url" ]; then
        export VIBE_USER_API_URL="$_url"
    fi
fi
if [ -n "${VIBE_USER_API_URL:-}" ]; then
    ok "VIBE_USER_API_URL: 编译期注入 ${VIBE_USER_API_URL}"
else
    warn "VIBE_USER_API_URL 未设置，产物将回退 http://127.0.0.1:8001"
fi

BUILD_START=$(date +%s)

( cd "$SRC_TAURI" && cargo tauri build ) || { err "cargo tauri build 失败"; exit 3; }

BUILD_END=$(date +%s)
ok "构建完成，耗时 $((BUILD_END - BUILD_START))s"

# ── 定位产物 ─────────────────────────────────────────────────
BUNDLE_DIR="$SRC_TAURI/target/release/bundle"
APP_PATH="$BUNDLE_DIR/macos/Trading Worker.app"
DMG_DIR="$BUNDLE_DIR/dmg"
DMG_PATH="$(ls -1 "$DMG_DIR"/*.dmg 2>/dev/null | head -1 || true)"

if [ -z "$DMG_PATH" ] || [ ! -f "$DMG_PATH" ]; then
    err "未找到 .dmg 产物（预期位置: $DMG_DIR/）"
    exit 3
fi
ok ".app → $APP_PATH"
ok ".dmg → $DMG_PATH"

# ── 冒烟检查 ─────────────────────────────────────────────────
if [ "$SKIP_SMOKE" -eq 0 ]; then
    section "冒烟检查"

    # 1) .app 主可执行位
    APP_BIN="$APP_PATH/Contents/MacOS/trading-worker"
    if [ -x "$APP_BIN" ]; then
        ok "主可执行文件存在且有执行位"
    else
        err "主可执行文件缺失或不可执行: $APP_BIN"
        exit 4
    fi

    # 2) 内嵌图标体积（占位符 icns 约 117 字节；规范版本应为 MB 级）
    APP_ICNS="$APP_PATH/Contents/Resources/icon.icns"
    if [ -f "$APP_ICNS" ]; then
        ICNS_SIZE=$(stat -f%z "$APP_ICNS" 2>/dev/null || stat -c%s "$APP_ICNS" 2>/dev/null)
        if [ "$ICNS_SIZE" -gt 100000 ]; then
            ok "icon.icns ($ICNS_SIZE bytes) — 符合规范"
        else
            warn "icon.icns 仅 $ICNS_SIZE bytes — 疑似占位符，请检查图标生成"
        fi
    else
        err "icon.icns 缺失"
        exit 4
    fi

    # 3) Info.plist 图标引用
    if grep -q "CFBundleIconFile" "$APP_PATH/Contents/Info.plist" 2>/dev/null; then
        ok "Info.plist 已声明 CFBundleIconFile"
    else
        warn "Info.plist 未找到 CFBundleIconFile"
    fi

    # 4) 打包资源齐全（python-runtime / agent / frontend dist / pi）
    RES="$APP_PATH/Contents/Resources"
    for r in python-runtime agent frontend/dist pi; do
        if [ -e "$RES/$r" ]; then ok "resource ✓ $r"; else err "resource ✗ $r 缺失"; exit 4; fi
    done

    ok "冒烟检查全部通过"
fi

# ── 签名 + 公证（可选）──────────────────────────────────────
# 检测到 APPLE_SIGNING_IDENTITY 即走完整 Developer ID 签名 + notarization；
# 否则产物保持 ad-hoc 签名 —— 经浏览器下载分发到其他 Mac 会报"已损坏"。
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
    section "签名 + 公证 (Developer ID)"
    log "bash scripts/desktop/sign-and-notarize.sh"
    if ! bash "$ROOT/scripts/desktop/sign-and-notarize.sh"; then
        err "签名/公证失败"
        exit 5
    fi
    ok "签名 + 公证完成"
else
    warn "未设置 APPLE_SIGNING_IDENTITY — 产物未签名/未公证（MVP 默认模式）。"
    log "分发提示：用户从浏览器下载 DMG 后双击会报『已损坏』，请在 Release 说明里提供："
    printf '    xattr -cr "/Applications/Trading Worker.app"\n'
    log "完整安装说明见 docs/desktop/README.md；购入 Apple 开发者账号后配置证书即可一键启用签名公证。"
fi

# ── 摘要 ─────────────────────────────────────────────────────
section "构建摘要"

DMG_SIZE_HUMAN=$(du -h "$DMG_PATH" | cut -f1)
DMG_SIZE_BYTES=$(stat -f%z "$DMG_PATH" 2>/dev/null || stat -c%s "$DMG_PATH" 2>/dev/null)
VERSION="$(cat "$BUILD/VERSION" 2>/dev/null || echo unknown)"
GIT_SHA="$(cd "$ROOT" && git rev-parse --short HEAD 2>/dev/null || echo n/a)"

printf "${C_BOLD}产物:${C_RESET}\n"
printf "  .dmg    : %s\n" "$DMG_PATH"
printf "  .app    : %s\n" "$APP_PATH"
printf "  大小    : %s (%s bytes)\n" "$DMG_SIZE_HUMAN" "$DMG_SIZE_BYTES"
printf "  版本标记: %s\n" "$VERSION"
printf "  Git SHA : %s\n" "$GIT_SHA"
printf "\n${C_GREEN}${C_BOLD}✓ 构建成功${C_RESET}\n"
printf "打开安装:\n  open \"%s\"\n" "$DMG_PATH"
printf "上传 Release:\n  bash scripts/desktop/upload-release.sh\n"
