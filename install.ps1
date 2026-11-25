# Entryx: установка одной командой на Windows (Docker Desktop + Python 3.10+).
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Повторный запуск безопасен: готовые шаги пропускаются, пароли и ключи не перезаписываются.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Root = $PSScriptRoot

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Get-RandomHex([int]$bytes) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $_.ToString("x2") })
}
function Get-RandomBase64([int]$bytes) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    [Convert]::ToBase64String($b)
}
function Read-EnvValue($file, $key) {
    if (-not (Test-Path $file)) { return "" }
    foreach ($line in [IO.File]::ReadAllLines($file)) {
        if ($line -match "^$key=(.*)$") { return $Matches[1].Trim() }
    }
    return ""
}
function Set-EnvValue($file, $key, $value) {
    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path $file) { $lines.AddRange([IO.File]::ReadAllLines($file)) }
    $i = $lines.FindIndex({ param($l) $l -match "^$key=" })
    if ($i -ge 0) { $lines[$i] = "$key=$value" } else { $lines.Add("$key=$value") }
    [IO.File]::WriteAllLines($file, $lines, (New-Object Text.UTF8Encoding $false))
}
function Copy-IfMissing($from, $to) {
    if (-not (Test-Path $to)) { Copy-Item $from $to; Write-Host "  создан $to" }
}
function Wait-Http($url, $name, [int]$minutes = 10) {
    $deadline = (Get-Date).AddMinutes($minutes)
    while ((Get-Date) -lt $deadline) {
        try { $null = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5; return } catch { }
        Write-Host "  жду $name…"
        Start-Sleep -Seconds 10
    }
    throw "$name не ответил за $minutes мин ($url). Посмотрите журнал: docker compose logs"
}
function Invoke-Native([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Команда завершилась с ошибкой: $exe $($arguments -join ' ')" }
}

# --- 0. Что нужно на компьютере -----------------------------------------------
Write-Step "Проверяю Docker и Python"
try { docker version --format "{{.Server.Version}}" | Out-Null } catch { throw "Docker не запущен. Установите и запустите Docker Desktop." }
if ($LASTEXITCODE -ne 0) { throw "Docker не запущен. Установите и запустите Docker Desktop." }
docker compose version | Out-Null
$Python = $null
foreach ($cand in @("py", "python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        $args0 = if ($cand -eq "py") { @("-3", "-c", "import sys; print(sys.version_info >= (3, 10))") } else { @("-c", "import sys; print(sys.version_info >= (3, 10))") }
        $ok = & $cand @args0 2>$null
        if ($ok -eq "True") { $Python = $cand; break }
    }
}
if (-not $Python) { throw "Нужен Python 3.10 или новее: https://www.python.org/downloads/" }

# --- 1. Пароли и .env ---------------------------------------------------------
Write-Step "Готовлю пароли и настройки (.env)"
New-Item -ItemType Directory -Force (Join-Path $Root ".secrets") | Out-Null
$Acc = Join-Path $Root ".secrets\local-accounts.env"
Copy-IfMissing (Join-Path $Root "local-accounts.env.example") $Acc
if (-not (Read-EnvValue $Acc "DIFY_PASSWORD")) { Set-EnvValue $Acc "DIFY_PASSWORD" ("Entryx" + (Get-RandomHex 8) + "7") }

$DifyEnv = Join-Path $Root "dify\.env"
Copy-IfMissing (Join-Path $Root "dify\.env.example") $DifyEnv
if (-not (Read-EnvValue $DifyEnv "DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN")) { Set-EnvValue $DifyEnv "DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN" (Get-RandomHex 32) }
# Агенты Dify читают сделки у моста на этом компьютере: разрешаем прокси Dify только этот адрес.
Set-EnvValue $DifyEnv "SSRF_PROXY_ALLOW_PRIVATE_DOMAINS" "host.docker.internal"

$CrmEnv = Join-Path $Root "crm\.env"
Copy-IfMissing (Join-Path $Root "crm\.env.example") $CrmEnv
if (-not (Read-EnvValue $CrmEnv "PG_DATABASE_PASSWORD")) { Set-EnvValue $CrmEnv "PG_DATABASE_PASSWORD" (Get-RandomHex 24) }
if (-not (Read-EnvValue $CrmEnv "ENCRYPTION_KEY")) { Set-EnvValue $CrmEnv "ENCRYPTION_KEY" (Get-RandomBase64 32) }

$BridgeEnv = Join-Path $Root "bridge\.env"
Copy-IfMissing (Join-Path $Root "bridge\.env.example") $BridgeEnv

# --- 2. Python для скриптов настройки -----------------------------------------
Write-Step "Ставлю Python-зависимости в .venv"
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    if ($Python -eq "py") { Invoke-Native "py" @("-3", "-m", "venv", ".venv") } else { Invoke-Native $Python @("-m", "venv", ".venv") }
}
Invoke-Native $VenvPy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", "bridge\requirements.txt")

# --- 3. Dify и Twenty в Docker -------------------------------------------------
Write-Step "Запускаю Dify (первый раз скачивает образы, 5–15 минут)"
Push-Location (Join-Path $Root "dify"); try { Invoke-Native "docker" @("compose", "up", "-d") } finally { Pop-Location }
Write-Step "Запускаю Twenty CRM"
Push-Location (Join-Path $Root "crm"); try { Invoke-Native "docker" @("compose", "up", "-d") } finally { Pop-Location }

# --- 4. Dify: администратор, плагин Gemini, 7 агентов, ключи в bridge/.env ------
Write-Step "Настраиваю Dify: администратор, плагин Gemini, агенты"
Invoke-Native $VenvPy @("tools\dify_setup.py")

# --- 5. Twenty: аккаунт и API-ключ (один раз, в браузере) ----------------------
Write-Step "Twenty CRM"
Wait-Http "http://localhost:3000/healthz" "Twenty"
if (-not (Read-EnvValue $BridgeEnv "TWENTY_API_KEY")) {
    Write-Host @"
  Один раз вручную (Twenty не даёт сделать это скриптом):
    1. Откроется http://localhost:3000 — зарегистрируйтесь и создайте рабочее пространство.
    2. Settings → APIs & Webhooks → Create API key (срок — подольше).
    3. Скопируйте ключ и вставьте сюда.
"@ -ForegroundColor Yellow
    Start-Process "http://localhost:3000"
    $key = ""
    while (-not $key) { $key = (Read-Host "  API key Twenty").Trim() }
    Set-EnvValue $BridgeEnv "TWENTY_API_KEY" $key
}
Write-Step "Настраиваю воронку и поля сделки в Twenty"
Push-Location (Join-Path $Root "bridge"); try { Invoke-Native $VenvPy @("setup_crm.py") } finally { Pop-Location }

# --- 6. Мост --------------------------------------------------------------------
Write-Step "Запускаю мост Entryx"
Push-Location (Join-Path $Root "bridge"); try { Invoke-Native "docker" @("compose", "up", "-d", "--build") } finally { Pop-Location }
Wait-Http "http://localhost:8787/health" "мост" 3

# --- Готово -----------------------------------------------------------------------
Write-Host @"

Готово.
  Чат Entryx (роль выбирается в шапке):  http://localhost:8787/app
  Трекер проектов:                       http://localhost:8787/
  Dify:                                  http://localhost       (вход: .secrets\local-accounts.env)
  Twenty CRM:                            http://localhost:3000

Остался один шаг руками: ключ модели.
  Dify → аватар → Настройки → Поставщик моделей → Gemini → ключ Google AI Studio.
  Без ключа кнопки и задачи работают, а ответы на свободные вопросы — нет.
"@ -ForegroundColor Green
