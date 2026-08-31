# 导出已安装软件清单 + 系统集成点，供归因匹配使用（只读）
# 注意：本脚本禁止出现非 ASCII 路径字面量。
# PowerShell 5.1 按 ANSI(GBK) 解码无 BOM 的 UTF-8 脚本，中文路径会变乱码。
# 输出路径经 -OutFile 参数传入（调用方给绝对路径），不给则退回 $PSScriptRoot。
# 为什么要参数化：安装到 Program Files 或打成 PyInstaller 单 exe 之后，脚本所在
# 目录不可写，而用户数据本就该落 %LOCALAPPDATA%（见 lostpath/storage/paths.py）。
# 走参数而非脚本内字面量，中文路径也不会被 GBK 解码坑到。
param([string]$OutFile = '')
$ErrorActionPreference = 'Stop'
$out = @{}

function Get-ExePath($s) {
    if (-not $s) { return $null }
    $t = $s.Trim()
    if ($t.StartsWith('"')) { $i = $t.IndexOf('"', 1); if ($i -gt 0) { return $t.Substring(1, $i - 1) } }
    if ($t -match '^\\\?\?\\([A-Za-z]:\\.*?\.exe)') { return $Matches[1] }
    if ($t -match '^([A-Za-z]:\\[^ ]*?\.exe)') { return $Matches[1] }
    if ($t -match '^([A-Za-z]:\\.*?\.exe)') { return $Matches[1] }
    return $null
}

$paths = @(
    @{k = 'HKLM64'; p = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' },
    @{k = 'HKLM32'; p = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' },
    @{k = 'HKCU'; p = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' }
)
$apps = @()
foreach ($e in $paths) {
    Get-ItemProperty $e.p -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName } | ForEach-Object {
        $loc = $_.InstallLocation
        if ($loc) { $loc = $loc.Trim().Trim('"') }
        $apps += [pscustomobject]@{
            hive            = $e.k
            key             = $_.PSChildName
            name            = $_.DisplayName
            publisher       = $_.Publisher
            version         = $_.DisplayVersion
            installLocation = $loc
            uninstallExe    = (Get-ExePath $_.UninstallString)
            estimatedSizeKB = $_.EstimatedSize
        }
    }
}
$out.apps = $apps

$out.services = @(Get-CimInstance Win32_Service -ErrorAction SilentlyContinue | ForEach-Object {
        $p = Get-ExePath $_.PathName
        if ($p) { [pscustomobject]@{ name = $_.Name; display = $_.DisplayName; exe = $p; state = $_.State; start = $_.StartMode } }
    })

$runKeys = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run')
$runs = @()
foreach ($k in $runKeys) {
    $it = Get-Item $k -ErrorAction SilentlyContinue
    if ($it) { foreach ($v in $it.GetValueNames()) { $runs += [pscustomobject]@{ key = $k; name = $v; raw = $it.GetValue($v); exe = (Get-ExePath $it.GetValue($v)) } } }
}
$out.startup = $runs

$ap = @()
foreach ($k in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\*')) {
    Get-ItemProperty $k -ErrorAction SilentlyContinue | ForEach-Object { $ap += [pscustomobject]@{ name = $_.PSChildName; exe = (Get-ExePath $_.'(default)') } }
}
$out.appPaths = $ap

$out.appx = @(Get-AppxPackage -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject]@{ name = $_.Name; publisher = $_.Publisher; family = $_.PackageFamilyName; install = $_.InstallLocation }
    })

$out.tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } | ForEach-Object {
        $acts = @($_.Actions | ForEach-Object { $_.Execute })
        [pscustomobject]@{ name = $_.TaskName; path = $_.TaskPath; actions = $acts }
    })

# 刻意不采集环境变量。
#
# 这里曾用 [Environment]::GetEnvironmentVariables() 整块收 User + Machine 两级，
# 想法是"环境变量可以当归因证据"。但归因实际走的是另一条路——lostpath/act/envvar.py
# 在需要时从注册表读**当前值**，因为快照里的旧值可能早已过时。判据是删掉这块之后
# 全套测试仍然全过：它从采集之日起就没有任何消费者。
#
# 代价却是实打实的：整块环境变量里会有 API key、token 这类凭据（本仓库的归因基准
# 就因此在 tests/fixtures/ 里带过一个真 key 进 git），还有完整的 Path——足以列出
# 这台机器装了哪些开发工具。**零收益的暴露面就该直接不要。**
#
# 以后真需要某个具体变量，按名字单独读，别整块抓。

if ($OutFile) { $dst = $OutFile } else { $dst = Join-Path $PSScriptRoot 'inventory.json' }
$json = $out | ConvertTo-Json -Depth 6 -Compress
[IO.File]::WriteAllText($dst, $json, (New-Object Text.UTF8Encoding($false)))
"apps=$($apps.Count) services=$($out.services.Count) startup=$($runs.Count) appPaths=$($ap.Count) appx=$($out.appx.Count) tasks=$($out.tasks.Count)"
"written: $dst"
