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

# QuantFund_KlineEvening 21:30 板块 K 线晚间补拉（2026-09-08 新增）。
# 与 run.py 体系不同：直接跑补拉脚本（节假日判断在脚本内部，读 data/holidays.json）。
# 背景：09-05/09-08 两次东财频控都在 16:00 撞车，21:30 补拉让 22:30 Shadow 当晚可读全量。
# 2026-09-08 夜 V3 P0-3：显式传 --trigger scheduler。evening 脚本 --trigger 默认 manual，
#   日志来源标注因此不再靠硬编码自称计划任务（旧 evening 第 125 行的审计缺陷）。
#   注：09-09 09:45 已经 Summer 授权实际执行本脚本重注册（QuantFund_KlineEvening 现带
#   --trigger scheduler；重注册前配置备份 backups/win_tasks_pre_reregister_20260909/）。
$evenPy   = Join-Path $proj "pull_sector_klines_evening.py"
if (Test-Path $evenPy) {
    $action  = New-ScheduledTaskAction -Execute $py `
                -Argument "-X utf8 `"$evenPy`" --trigger scheduler" `
                -WorkingDirectory $proj
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "21:30"
    Register-ScheduledTask -TaskName "QuantFund_KlineEvening" -Action $action -Trigger $trigger -Force | Out-Null
    Write-Host "registered: QuantFund_KlineEvening at 21:30 (Mon-Fri)"
} else {
    Write-Warning "pull_sector_klines_evening.py not found, skip QuantFund_KlineEvening"
}
Write-Host "done. next weekday 11:30 will fire QuantFund_Mid."
