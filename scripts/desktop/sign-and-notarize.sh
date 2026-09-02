#!/usr/bin/env bash
# scripts/desktop/sign-and-notarize.sh
#
# 对 `cargo tauri build` 产出的 .app / .dmg 做完整的 Apple 代码签名 + 公证 + 装订，
# 解决"经浏览器下载后 macOS 报『安装包已损坏』"的问题。
#
# 为什么需要这个脚本：
#   Tauri 默认产物是 ad-hoc 签名（Signature=adhoc），未公证。经浏览器下载的 DMG
#   会被打上 com.apple.quarantine，Gatekeeper 对"未签名 + 未公证 + 隔离"一律报
#   "已损坏"。本脚本用 Developer ID 证书深签名（含内嵌 Python runtime 的 400+ 个
#   动态库）+ notarization + stapler 装订，让终端用户双击即可安装。
#
# 作用范围：
#   1. 校验凭据（APPLE_SIGNING_IDENTITY 必填；公证凭据 keychain profile 或明文三件套）
#   2. 校验 keychain 内存在该 Developer ID 证书
#   3. 深签名 .app：由内向外（.dylib/.so → python3 → 主 bundle），全部带 Hardened Runtime
#   4. codesign --verify 校验签名完整性
#   5. 签名 .dmg
#   6. xcrun notarytool submit --wait 上传 Apple 公证
#   7. xcrun stapler staple 装订公证票据到 .dmg（+ .app）
#   8. spctl / stapler validate 终极验证
#
# 用法：
#   APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   APPLE_KEYCHAIN_PROFILE=vibe-trading \
#   bash scripts/desktop/sign-and-notarize.sh
#
#   或明文公证凭据：
#   APPLE_SIGNING_IDENTITY="..." APPLE_ID=you@example.com \
#   APPLE_APP_SPECIFIC_PASSWORD=xxxx-xxxx-xxxx-xxxx APPLE_TEAM_ID=TEAMID \
#   bash scripts/desktop/sign-and-notarize.sh
#
#   只签名不公证（调试用）：
#   APPLE_SIGNING_IDENTITY="..." APPLE_SKIP_NOTARIZE=1 bash scripts/desktop/sign-and-notarize.sh
#
# 前置条件：
#   - 已运行 build-dmg.sh，.app 与 .dmg 产物就绪
#   - Developer ID Application 证书已导入登录钥匙串
#   - 已通过 `xcrun notarytool store-credentials` 建立 keychain profile，
#     或持有 App 专用密码（appleid.apple.com 生成）
#
# 退出码：
#   0 成功 / 1 凭据/产物缺失 / 2 签名失败 / 3 公证失败 / 4 装订/验证失败

set -euo pipefail

# ── 路径与颜色 ───────────────────────────────────────────────
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_TAURI="$ROOT/src-tauri"
APP_PATH="$SRC_TAURI/target/release/bundle/macos/Trading Worker.app"
DMG_DIR="$SRC_TAURI/target/release/bundle/dmg"
ENT="$SRC_TAURI/Entitlements.plist"

if [ -t 1 ]; then
    C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'
    C_YELLOW='\033[33m'; C_RED='\033[31m'; C_BLUE='\033[4m'; C_CYAN='\033[36m'
else
    C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_BLUE=''; C_CYAN=''
fi

log()  { printf "${C_CYAN}▸${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_GREEN}✓${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}!${C_RESET} %s\n" "$*" >&2; }
err()  { printf "${C_RED}✗${C_RESET} %s\n" "$*" >&2; }
section() { printf "\n${C_BOLD}${C_BLUE}━━━ %s ━━━${C_RESET}\n" "$*"; }

# ── 1. 校验凭据 ──────────────────────────────────────────────
section "凭据检查"

IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
    err "未设置 APPLE_SIGNING_IDENTITY。"
    err "在钥匙串里执行 \`security find-identity -p codesigning -v\` 复制 Developer ID Application 行。"
    exit 1
fi
ok "签名身份: $IDENTITY"

# 公证凭据：keychain profile（推荐）或 明文三件套
SKIP_NOTARIZE="${APPLE_SKIP_NOTARIZE:-0}"
NOTARY_ARGS=""
if [ "$SKIP_NOTARIZE" = "1" ]; then
    warn "APPLE_SKIP_NOTARIZE=1 — 仅签名，不公证（产物仍会被 Gatekeeper 拦，仅供调试）。"
elif [ -n "${APPLE_KEYCHAIN_PROFILE:-}" ]; then
    NOTARY_ARGS=(--keychain-profile "$APPLE_KEYCHAIN_PROFILE")
    ok "公证凭据: keychain profile '$APPLE_KEYCHAIN_PROFILE'"
elif [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ]; then
    NOTARY_ARGS=(--apple-id "$APPLE_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" --team-id "$APPLE_TEAM_ID")
    ok "公证凭据: Apple ID $APPLE_ID (team $APPLE_TEAM_ID)"
else
    err "缺少公证凭据。请设置 APPLE_KEYCHAIN_PROFILE，或 APPLE_ID + APPLE_APP_SPECIFIC_PASSWORD + APPLE_TEAM_ID。"
    err "（调试可设 APPLE_SKIP_NOTARIZE=1 仅签名不公证）"
    exit 1
fi

# ── 2. 校验产物 ──────────────────────────────────────────────
section "产物检查"
if [ ! -d "$APP_PATH" ]; then
    err "未找到 .app: $APP_PATH"
    err "请先运行 bash scripts/desktop/build-dmg.sh"
    exit 1
fi
if [ ! -f "$ENT" ]; then
    err "未找到 Entitlements: $ENT"
    exit 1
fi
DMG_PATH="$(ls -1 "$DMG_DIR"/*.dmg 2>/dev/null | head -1 || true)"
if [ -z "$DMG_PATH" ] || [ ! -f "$DMG_PATH" ]; then
    err "未找到 .dmg: $DMG_DIR/*.dmg"
    err "请先运行 bash scripts/desktop/build-dmg.sh"
    exit 1
fi
ok ".app → $APP_PATH"
ok ".dmg → $DMG_PATH"

# ── 3. 校验证书在钥匙串 ──────────────────────────────────────
section "钥匙串证书检查"
if ! security find-identity -p codesigning -v 2>/dev/null | grep -qF "$IDENTITY"; then
    err "钥匙串未找到该签名身份: $IDENTITY"
    err "可用身份:"
    security find-identity -p codesigning -v >&2 || true
    exit 1
fi
ok "证书已在钥匙串"

# codesign 公共参数
CS_RUNTIME=(--options runtime --timestamp --force --sign "$IDENTITY")

# ── 4. 深签名 .app（由内向外）─────────────────────────────────
section "深签名 .app（由内向外）"

# 4a. 所有 .dylib / .so（内嵌 Python runtime 的 400+ 个 C 扩展）
log "签名 .dylib / .so …"
DYLIB_COUNT=0
while IFS= read -r -d '' f; do
    codesign "${CS_RUNTIME[@]}" "$f"
    DYLIB_COUNT=$((DYLIB_COUNT + 1))
    if [ $((DYLIB_COUNT % 50)) -eq 0 ]; then log "  已签名 $DYLIB_COUNT 个动态库 …"; fi
done < <(find "$APP_PATH" -type f \( -name "*.dylib" -o -name "*.so" \) -print0)
ok "动态库签名完成（$DYLIB_COUNT 个）"

# 4b. framework 目录（若有；codesign 对 framework 目录整体签名）
FW_COUNT=0
while IFS= read -r -d '' fw; do
    [ -d "$fw" ] || continue
    codesign "${CS_RUNTIME[@]}" "$fw"
    FW_COUNT=$((FW_COUNT + 1))
done < <(find "$APP_PATH" -type d -name "*.framework" -print0)
if [ "$FW_COUNT" -gt 0 ]; then ok "framework 签名完成（$FW_COUNT 个）"; else ok "无 framework（跳过）"; fi

# 4c. python-runtime/bin 下的 Mach-O 可执行（python3.12 等，带 entitlements）
log "签名 Python 解释器可执行文件 …"
BIN_COUNT=0
while IFS= read -r -d '' b; do
    # 只签 Mach-O 二进制，跳过 python 脚本（pip 等）
    if file "$b" 2>/dev/null | grep -q "Mach-O"; then
        codesign "${CS_RUNTIME[@]}" --entitlements "$ENT" "$b"
        BIN_COUNT=$((BIN_COUNT + 1))
    fi
done < <(find "$APP_PATH/Contents/Resources/python-runtime/bin" -type f -print0 2>/dev/null)
ok "解释器可执行签名完成（$BIN_COUNT 个）"

# 4c.1 Pi sidecar 二进制（compiled Bun executable；必须随 app deep-sign，design §Packaging）
for pibin in "$APP_PATH"/Contents/Resources/pi/*/pi*; do
  [ -f "$pibin" ] || continue
  log "codesign pi binary: $pibin"
  codesign "${CS_RUNTIME[@]}" --entitlements "$ENT" "$pibin"
done

# 4d. 主 app bundle（最后；--deep 兜底覆盖 Contents/MacOS 主二进制与任何遗漏）
log "签名主 bundle …"
codesign "${CS_RUNTIME[@]}" --deep --entitlements "$ENT" "$APP_PATH"
ok "主 bundle 签名完成"

# ── 5. 签名校验 ───────────────────────────────────────────────
section "签名完整性校验"
if codesign --verify --deep --strict --verbose=2 "$APP_PATH" 2>&1; then
    ok ".app 签名校验通过"
else
    err ".app 签名校验失败"
    exit 2
fi

# ── 6. 签名 .dmg ──────────────────────────────────────────────
section "签名 .dmg"
# DMG 不需要 hardened runtime / entitlements（它不是可执行代码容器）
codesign --force --sign "$IDENTITY" --timestamp "$DMG_PATH"
ok ".dmg 签名完成"

# 公证 + 装订
if [ "$SKIP_NOTARIZE" = "1" ]; then
    section "跳过公证（调试模式）"
    warn "产物仅签名未公证，分发给其他 Mac 仍会被 Gatekeeper 拦。"
    warn "正式发布请去掉 APPLE_SKIP_NOTARIZE 重跑。"
else
    # ── 7. 公证 ────────────────────────────────────────────────
    section "公证 .dmg（notarytool submit --wait）"
    log "上传至 Apple 公证服务，通常 2–15 分钟，请耐心等待 …"
    # notarytool --wait 在公证失败时返回非 0（含 Invalid 状态），set -e 会捕获
    if xcrun notarytool submit "$DMG_PATH" "${NOTARY_ARGS[@]}" --wait; then
        ok "公证通过"
    else
        err "公证失败。可前往 Apple 网站或用 \`xcrun notarytool log <submission-id> ${NOTARY_ARGS[*]}\` 查看详细日志。"
        exit 3
    fi

    # ── 8. 装订票据 ────────────────────────────────────────────
    section "装订公证票据（stapler）"
    xcrun stapler staple "$DMG_PATH"
    ok ".dmg 票据装订完成"
    # app 装订为可选：走 DMG 分发时 DMG 的票据足够；直接分发 .app 时才需要
    if xcrun stapler staple "$APP_PATH" 2>/dev/null; then
        ok ".app 票据装订完成"
    else
        warn ".app 票据装订跳过（走 DMG 分发不影响；仅直接分发 .app 时需要）"
    fi
fi

# ── 9. 终极验证 ───────────────────────────────────────────────
section "终极验证（模拟 Gatekeeper）"
FAIL=0

log "① stapler validate .dmg"
if xcrun stapler validate "$DMG_PATH"; then ok ".dmg 票据有效"; else err ".dmg 票据无效"; FAIL=1; fi

log "② spctl 评估 .dmg（install 类型）"
if SPCTL_OUT=$(spctl -a -vvv -t install "$DMG_PATH" 2>&1); then
    printf "  %s\n" "$SPCTL_OUT"
    if echo "$SPCTL_OUT" | grep -q "Notarized Developer ID"; then
        ok "Gatekeeper 认可：source=Notarized Developer ID"
    else
        warn "Gatekeeper 接受但来源非 Notarized Developer ID，请检查"
    fi
else
    err "spctl 拒绝该 DMG"
    printf "  %s\n" "$SPCTL_OUT" >&2
    FAIL=1
fi

log "③ codesign --verify --deep --strict .app"
if codesign --verify --deep --strict "$APP_PATH" 2>/dev/null; then
    ok ".app 深度签名完整"
else
    err ".app 签名不完整"
    FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
    err "终极验证未全部通过"
    exit 4
fi

# ── 摘要 ─────────────────────────────────────────────────────
section "完成"
printf "${C_GREEN}${C_BOLD}✓ 签名 + 公证完成${C_RESET}\n"
printf "  .dmg : %s\n" "$DMG_PATH"
printf "  .app : %s\n" "$APP_PATH"
if [ "$SKIP_NOTARIZE" != "1" ]; then
    printf "\n现在可以把 .dmg 分发给任意 Mac：经浏览器下载后双击安装不会再报『已损坏』。\n"
fi
