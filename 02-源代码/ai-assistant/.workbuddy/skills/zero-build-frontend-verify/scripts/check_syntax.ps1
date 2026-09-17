# L1：零构建前端语法检查
#
# `node --check` 不能直接跑带 import 的 .js（会被当 CommonJS 解析并报错），
# 所以先把每个 .js 复制成 .mjs 再检查。
#
# 用法：
#   powershell -File check_syntax.ps1 -Dir <前端目录>
#   powershell -File check_syntax.ps1 -Dir .\frontend -Node <node.exe 路径>

param(
  [Parameter(Mandatory = $true)][string]$Dir,
  [string]$Node = "node"
)

$ErrorActionPreference = "Stop"
$dir = (Resolve-Path $Dir).Path
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("jscheck-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

$fail = 0
$count = 0
foreach ($f in (Get-ChildItem -Path $dir -Recurse -Filter *.js -File)) {
  $count++
  $flat = ($f.FullName.Substring($dir.Length).TrimStart('\')) -replace '[\\/]', '__'
  $dest = Join-Path $tmp ($flat + ".mjs")
  Copy-Item $f.FullName $dest -Force
  $out = & $Node --check $dest 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) {
    $fail++
    Write-Output "[FAIL] $flat"
    Write-Output ($out -split "`n" | Select-Object -First 8)
  }
}

Write-Output ""
if ($fail -eq 0) {
  Write-Output "[OK] $count 个文件语法检查全部通过"
} else {
  Write-Output "[FAIL] $count 个文件里有 $fail 个语法错误"
}
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
exit $(if ($fail -eq 0) { 0 } else { 1 })
