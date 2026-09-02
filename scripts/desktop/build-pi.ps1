# Build + stage the Pi sidecar binary for windows-x64.
# Usage: build-pi.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PiDir = Join-Path $Root "pi-sidecar"
$Out = Join-Path $Root ".desktop-build\pi"
Set-Location $PiDir
bun install --frozen-lockfile
# 发布闸门：内建工具封锁（write/edit/bash 不绕过 Python ToolGateway）是行为性
# 保证 —— toolNames:[] + plan.enabled:false 依赖 SDK 内部注册行为，非 API 级白名单。
# 编译前跑完整 sidecar 测试套件（含 session-driver.test.ts 的内建工具不在场断言）：
# SDK 行为漂移在此失败并中止构建，而不是静默进入发布包。
bun test
# omp-legacy-pi-modules: SDK 私有编译期虚拟模块（npm 包不含），仅在编译态 legacy
# 扩展加载路径用到；sidecar 只用 createAgentSession + 现代扩展，标记 external 保持
# 未解析即可（与 SDK 自身 bundle-dist.ts 的 ALWAYS_EXTERNAL 一致）。
# --outdir（非 --outfile）：SDK 根导出图内含模板/文档资产，会产出多文件。
Remove-Item -Recurse -Force "dist" -ErrorAction SilentlyContinue
bun build src/main.ts --outdir dist --target bun --external omp-legacy-pi-modules
New-Item -ItemType Directory -Force -Path (Join-Path $Out "extensions") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PiDir "dist\extensions") | Out-Null
# extension 产物同时：(a) 暂存到 $Out\extensions\（tauri 资源契约）；
# (b) 嵌入编译二进制 $bunfs/root/extensions/ —— main.ts 以 `${import.meta.dir}/extensions/*.js`
#     动态 import，编译态 import.meta.dir = /$bunfs/root，只有嵌入才能命中。
bun build extensions/vibe-memory/index.ts --outfile (Join-Path $Out "extensions\vibe-memory.js") --target bun
Copy-Item -Force (Join-Path $Out "extensions\vibe-memory.js") (Join-Path $PiDir "dist\extensions\vibe-memory.js")
bun build extensions/vibe-providers/index.ts --outfile (Join-Path $Out "extensions\vibe-providers.js") --target bun
Copy-Item -Force (Join-Path $Out "extensions\vibe-providers.js") (Join-Path $PiDir "dist\extensions\vibe-providers.js")
New-Item -ItemType Directory -Force -Path (Join-Path $Out "windows-x64") | Out-Null
bun build --compile dist/main.js dist/extensions/vibe-providers.js dist/extensions/vibe-memory.js `
  --target bun-windows-x64 --external omp-legacy-pi-modules --outfile (Join-Path $Out "windows-x64\pi.exe")
# N-API 原生插件：pi-natives（SDK npm 包 embeddedAddon=null，编译态二进制从 execDir
# 候选路径加载 pi_natives.win32-x64.node）；从 per-platform leaf 包拷贝到二进制旁。
$nativeSrc = Join-Path $PiDir "node_modules\@oh-my-pi\pi-natives-win32-x64\pi_natives.win32-x64.node"
if (Test-Path $nativeSrc) {
  Copy-Item -Force $nativeSrc (Join-Path $Out "windows-x64")
} else {
  Write-Warning "$nativeSrc not found - compiled binary will fail to load pi_natives (install the matching @oh-my-pi/pi-natives-win32-x64 leaf package)"
}
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Out "skills")
New-Item -ItemType Directory -Force -Path (Join-Path $Out "skills") | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "agent\src\skills\*") (Join-Path $Out "skills")
$lockHash = (Get-FileHash -Algorithm SHA256 (Join-Path $PiDir "bun.lock")).Hash.ToLower()
@{ pi_npm_version = "18.0.11"; bun_lock_sha256 = $lockHash; target = "windows-x64"; built_at = (Get-Date).ToUniversalTime().ToString("o") } |
  ConvertTo-Json | Set-Content (Join-Path $Out "PROVENANCE.json")
Write-Host "staged $Out\windows-x64\pi.exe"
