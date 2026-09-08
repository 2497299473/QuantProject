# 创建桌面快捷方式「基金日频参谋.lnk」→ 直接运行项目 venv 的 python（双击 = 自动判断时点运行）
# 普通 PowerShell 即可运行。
# 2026-09-08 迁移：入口由 wsl.exe 改为 Windows 原生；路径由 $PSScriptRoot 派生，换盘符不用改。
$proj  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py    = Join-Path $proj ".venv\Scripts\python.exe"
$runpy = Join-Path $proj "run.py"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }

$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = $ws.CreateShortcut((Join-Path $desktop "基金日频参谋.lnk"))
$lnk.TargetPath = $py
$lnk.Arguments = "-X utf8 `"$runpy`""
$lnk.WorkingDirectory = $proj
$lnk.Description = "Fund daily advisor - weak reference only"
$lnk.Save()
Write-Host "shortcut created: $(Join-Path $desktop '基金日频参谋.lnk')"
