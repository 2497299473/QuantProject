# 注册两个工作日定时任务（需管理员 PowerShell 运行）：
#   QuantFund_Mid  11:30 午盘实时参考
#   QuantFund_Post 14:55 收盘前最终参考
# 注意：任务仅在电脑开机且已登录时触发；错过（关机）默认不补跑。
# 直接以项目 venv 的 python.exe 为任务入口，不经过 .bat，减少一层转义。
# 2026-09-08 迁移：入口由 wsl.exe 改为 Windows 原生（项目根 = 本脚本上级目录）。
#   - 路径全部由 $PSScriptRoot 派生，换盘符/搬家都不用改
#   - -X utf8 强制 UTF-8 模式，避免 Windows 控制台 GBK 代码页下中文 print 抛 UnicodeEncodeError
$proj   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py     = Join-Path $proj ".venv\Scripts\python.exe"
$runpy  = Join-Path $proj "run.py"
if (-not (Test-Path $py))    { throw "venv python not found: $py" }
if (-not (Test-Path $runpy)) { throw "run.py not found: $runpy" }

$tasks = @(
    @{ Name = "QuantFund_Mid";  Time = "11:30"; Slot = "mid"  },
    @{ Name = "QuantFund_Post"; Time = "14:55"; Slot = "post" }
)
foreach ($t in $tasks) {
    $action  = New-ScheduledTaskAction -Execute $py `
                -Argument "-X utf8 `"$runpy`" --slot $($t.Slot)" `
                -WorkingDirectory $proj
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $t.Time
    # 节假日过滤由 run.py 内部判断（holidays.json），任务层面只排除周末
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Force | Out-Null
    Write-Host "registered: $($t.Name) at $($t.Time) (Mon-Fri) -> $py"
}
Write-Host "done. next weekday 11:30 will fire QuantFund_Mid."
