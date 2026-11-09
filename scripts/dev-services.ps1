<#
.SYNOPSIS
  启动本机开发依赖：WSL Ubuntu 中的 Redis 与 Docker Engine，并检查 Windows 上的 PostgreSQL 服务。

.DESCRIPTION
  WSL 在没有活动进程时会关闭发行版，其中由 systemd 管理的 Redis / Docker 也会随之停止。
  本脚本在后台保留一个隐藏的 WSL 进程使发行版保持运行，然后启动服务并等待 Redis 就绪。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\dev-services.ps1          # 启动
  powershell -ExecutionPolicy Bypass -File scripts\dev-services.ps1 -Stop    # 停止保活（WSL 空闲后自动关闭）
#>
param(
    [switch]$Stop,
    [string]$Distro = "Ubuntu",
    [int]$RedisPort = 6379
)

$ErrorActionPreference = "Stop"
$env:WSL_UTF8 = "1"
$marker = "device-agent-keepalive"

function Get-KeepAlive {
    Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | Where-Object { $_.CommandLine -like "*$marker*" }
}

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { return $client.ConnectAsync("127.0.0.1", $Port).Wait(500) -and $client.Connected }
    catch { return $false }
    finally { $client.Dispose() }
}

if ($Stop) {
    Get-KeepAlive | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "已停止 WSL 保活进程（WSL 空闲后会自动关闭 Redis / Docker）"
    return
}

if (-not (Get-KeepAlive)) {
    # 通过 WMI 创建进程：不属于当前终端的进程树，关闭终端/IDE 后仍会保留；窗口隐藏
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine               = "wsl.exe -d $Distro -- bash -c `"exec -a $marker sleep infinity`""
        ProcessStartupInformation = $startup
    }
    if ($result.ReturnValue -ne 0) { throw "启动 WSL 保活进程失败 (Win32_Process.Create=$($result.ReturnValue))" }
    Write-Host "已启动 WSL 保活进程 ($Distro, pid $($result.ProcessId))"
}

wsl.exe -d $Distro -u root -- systemctl start redis-server docker
if ($LASTEXITCODE -ne 0) { throw "在 WSL 中启动 redis-server / docker 失败" }

$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Port $RedisPort)) {
    if ((Get-Date) -gt $deadline) { throw "等待 Redis (localhost:$RedisPort) 超时" }
    Start-Sleep -Milliseconds 500
}
Write-Host "Redis   : localhost:$RedisPort 就绪"
Write-Host ("Docker  : " + (wsl.exe -d $Distro -u root -- docker version --format "{{.Server.Version}}"))

$pg = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $pg) {
    Write-Warning "未找到 PostgreSQL 服务（仅在使用 PostgreSQL 时需要）"
} elseif ($pg.Status -ne "Running") {
    Write-Warning "PostgreSQL 服务 $($pg.Name) 未运行，请在管理员 PowerShell 中执行: Start-Service $($pg.Name)"
} else {
    Write-Host "Postgres: $($pg.Name) 运行中 (localhost:5432)"
}
