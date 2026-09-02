# scripts/desktop/assemble.ps1
# 组装桌面打包资源到 .desktop-build\(供 tauri resources 引用)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Build = "$Root\.desktop-build"
$Runtime = "$Build\python-runtime"

# 1) 前端构建(复用现有 npm run build)
Write-Host "=== Building frontend ==="
Push-Location "$Root\frontend"
npm ci
npm run build
Pop-Location

# 1.1) 前端产物入台到 .desktop-build\frontend\dist
# dev 模式(cargo tauri dev)下 resources.rs 从这里取 res.frontend_dist,
# runtime_dir::prepare 每次启动都复制到 runtime 目录给 Python sidecar serve。
# release 由 tauri.conf.json bundle.resources 直接打包仓库根 frontend/dist,不走这里。
# 漏掉这一步 → .desktop-build\frontend\dist 不存在 → runtime_dir::prepare 跳过复制
# → Python 永远 serve runtime 里的旧 dist,dev 前端不更新。
Write-Host "=== Staging frontend/dist to .desktop-build\ ==="
if (Test-Path "$Build\frontend") { Remove-Item -Recurse -Force "$Build\frontend" }
New-Item -ItemType Directory -Force -Path "$Build\frontend" | Out-Null
Copy-Item -Path "$Root\frontend\dist" -Destination "$Build\frontend" -Recurse -Force

# 2) 运行时须已由 fetch-runtime.ps1 + install-deps.ps1 准备好
Write-Host "=== Checking runtime ==="
if (-not (Test-Path "$Runtime\python.exe")) { throw "runtime missing; run fetch-runtime.ps1 + install-deps.ps1 first" }

# 3) 裁剪运行时 site-packages 体积
Write-Host "=== Trimming runtime ==="
Get-ChildItem -Path $Runtime -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $Runtime -Recurse -Directory -Filter "tests" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $Runtime -Recurse -Directory -Filter "test" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 4) 准备 agent 代码模板
Write-Host "=== Preparing agent template ==="
if (Test-Path "$Build\agent") { Remove-Item -Recurse -Force "$Build\agent" }
Copy-Item -Recurse "$Root\agent" "$Build\agent"
foreach ($d in @("runs","sessions","uploads",".swarm")) {
    Remove-Item -Recurse -Force "$Build\agent\$d" -ErrorAction SilentlyContinue
}
Get-ChildItem -Path "$Build\agent" -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$Build\agent\tests" -ErrorAction SilentlyContinue

# 4.1) Pi sidecar staging (windows-x64)
Write-Host "=== Staging Pi sidecar ==="
& (Join-Path $PSScriptRoot "build-pi.ps1")

# 5) .env 种子
Write-Host "=== Preparing .env seed ==="
if (Test-Path "$Root\agent\.env") { Copy-Item "$Root\agent\.env" "$Build\agent\.env" }
elseif (Test-Path "$Root\agent\.env.example") { Copy-Item "$Root\agent\.env.example" "$Build\agent\.env" }
else { New-Item -ItemType File -Path "$Build\agent\.env" | Out-Null }

# 6) VERSION 标记
Write-Host "=== Creating VERSION marker ==="
if ($env:DESKTOP_RELEASE_VERSION) {
    $VersionMarker = $env:DESKTOP_RELEASE_VERSION
} else {
    $Commit = (git -C $Root rev-parse --short HEAD)
    $Timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
    $VersionMarker = "$Commit-$Timestamp"
}
$VersionMarker | Set-Content "$Build\VERSION"
Write-Host "VERSION -> $VersionMarker"

Write-Host "=== Assembly complete ==="
Write-Host "Contents of ${Build}:"
Get-ChildItem $Build | ForEach-Object { $size = (Get-ChildItem $_.FullName -Recurse -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum; "$($_.Name): $([math]::Round($size/1MB, 1))MB" }
