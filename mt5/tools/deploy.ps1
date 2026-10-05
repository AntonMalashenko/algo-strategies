<#
.SYNOPSIS
  One-command deploy of the MQL5 code into a MetaTrader 5 terminal on Windows
  (the Windows counterpart of mt5/tools/deploy.sh).

.DESCRIPTION
  1. regenerate the GENERATED headers (python -m mt5.tools.gen_params)
  2. run the mt5 pytest suite                       (skip: -SkipTests)
  3. generate self-test fixtures / tester history if missing
  4. mirror sources, presets and fixtures into the terminal (robocopy, .ex5 kept)
  5. compile every .mq5 we own with MetaEditor64.exe /compile and fail on errors
  6. -Watch: repeat 1-5 whenever a source file changes (Ctrl+C to stop)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File mt5\tools\deploy.ps1
  powershell -ExecutionPolicy Bypass -File mt5\tools\deploy.ps1 -Watch
  powershell -ExecutionPolicy Bypass -File mt5\tools\deploy.ps1 -DataDir "C:\Users\me\AppData\Roaming\MetaQuotes\Terminal\<hash>"

.NOTES
  Data folder = MT5: File > Open Data Folder. Autodetected as the most recently
  used %APPDATA%\MetaQuotes\Terminal\<hash> that contains an MQL5 folder; pass
  -DataDir when several terminals are installed. The install folder (where
  MetaEditor64.exe lives) is read from <DataDir>\origin.txt; for a /portable
  install DataDir is the install folder itself.
#>
param(
    [switch]$Watch,
    [switch]$SkipTests,
    [switch]$NoCompile,
    [string]$DataDir = "",
    [string]$CommonDir = ""
)
$ErrorActionPreference = "Stop"

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Mt5 = Join-Path $Repo "mt5"
$OwnTrees = @("Include\AlgoCore", "Include\Strategies", "Experts\AlgoTrading", "Scripts\AlgoTrading")
$OwnSources = @("Experts\AlgoTrading", "Scripts\AlgoTrading")
$WatchIntervalSeconds = 3
$RobocopyFailureCode = 8          # robocopy: exit codes >= 8 are failures

function Log([string]$Message) { Write-Host "[deploy] $Message" -ForegroundColor Cyan }

function Get-Python {
    if ($env:MT5_PYTHON) { return $env:MT5_PYTHON }
    $venv = Join-Path $Repo ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    return "python"
}

function Invoke-Python([string[]]$Arguments) {
    Push-Location $Repo
    try {
        & (Get-Python) @Arguments
        if ($LASTEXITCODE -ne 0) { throw "python $($Arguments -join ' ') failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
}

function Resolve-DataDir {
    if ($DataDir) { return $DataDir }
    $root = Join-Path $env:APPDATA "MetaQuotes\Terminal"
    $candidate = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "MQL5") } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $candidate) { throw "MT5 data folder not found -- pass -DataDir (File > Open Data Folder)" }
    return $candidate.FullName
}

function Resolve-InstallDir([string]$Data) {
    $origin = Join-Path $Data "origin.txt"
    if (Test-Path $origin) { return (Get-Content $origin -Encoding Unicode -TotalCount 1).Trim() }
    return $Data
}

function Resolve-CommonDir {
    if ($CommonDir) { return $CommonDir }
    return Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files"
}

function Sync-Tree([string]$Source, [string]$Target) {
    robocopy $Source $Target /MIR /XF *.ex5 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge $RobocopyFailureCode) { throw "robocopy $Source -> $Target failed ($LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

function Step-Generate {
    Log "1/5 generate headers"
    Invoke-Python @("-m", "mt5.tools.gen_params")
}

function Step-Tests {
    if ($SkipTests) { Log "2/5 tests skipped"; return }
    Log "2/5 pytest tests/mt5"
    Invoke-Python @("-m", "pytest", "tests/mt5", "-q", "-p", "no:cacheprovider")
}

function Step-Fixtures {
    $files = Join-Path $Mt5 "MQL5\Files\AlgoTrading"
    if (-not (Test-Path (Join-Path $files "fixtures\s021_m1.csv"))) {
        Log "3/5 self-test fixtures missing -> generating"
        Invoke-Python @("-m", "mt5.tools.s021_fixtures")
    } else { Log "3/5 fixtures present" }
    if (-not (Test-Path (Join-Path $files "e2e\s021_m1.csv"))) {
        Log "    tester history missing -> generating (2025-01-02..2026-09-30)"
        Invoke-Python @("-m", "mt5.tools.s021_fixtures", "--start", "2025-01-02", "--end", "2026-09-30",
                        "--out", (Join-Path $files "e2e"))
    }
}

function Step-Install([string]$Data, [string]$Common) {
    Log "4/5 install into $Data"
    foreach ($tree in $OwnTrees) {
        Sync-Tree (Join-Path $Mt5 "MQL5\$tree") (Join-Path $Data "MQL5\$tree")
        Log "    copy MQL5\$tree"
    }
    $presets = Join-Path $Mt5 "MQL5\Presets\AlgoTrading"
    foreach ($target in @("MQL5\Presets", "MQL5\Profiles\Tester")) {
        $dest = Join-Path $Data $target
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Copy-Item (Join-Path $presets "*.set") $dest -Force
    }
    Log "    copy presets -> MQL5\Presets, MQL5\Profiles\Tester"
    foreach ($sub in @("fixtures", "e2e")) {
        $source = Join-Path $Mt5 "MQL5\Files\AlgoTrading\$sub"
        if (Test-Path $source) {
            Sync-Tree $source (Join-Path $Common "AlgoTrading\$sub")
            Log "    copy $sub -> $Common\AlgoTrading\$sub"
        }
    }
}

function Step-Compile([string]$Data, [string]$Install) {
    if ($NoCompile) { Log "5/5 compile skipped"; return }
    $editor = Join-Path $Install "MetaEditor64.exe"
    if (-not (Test-Path $editor)) { Log "5/5 MetaEditor64.exe not found in $Install -- compile with F7"; return }
    Log "5/5 compile with $editor"
    $failed = 0
    foreach ($dir in $OwnSources) {
        Get-ChildItem (Join-Path $Data "MQL5\$dir") -Filter *.mq5 | Sort-Object Name | ForEach-Object {
            $log = Join-Path $env:TEMP "algotrading_compile_$($_.BaseName).log"
            Remove-Item $log -ErrorAction SilentlyContinue
            Start-Process -FilePath $editor -ArgumentList "/compile:`"$($_.FullName)`"", "/log:`"$log`"" -Wait -NoNewWindow
            $result = if (Test-Path $log) { Get-Content $log -Encoding Unicode | Where-Object { $_ -like "Result:*" } | Select-Object -Last 1 } else { "" }
            if ($result -like "*: 0 errors*" -or $result -like "Result: 0 errors*") {
                Write-Host "  ok   $dir\$($_.Name)  ($result)"
            } else {
                $failed++
                Write-Host "  FAIL $dir\$($_.Name)  ($result)" -ForegroundColor Red
                if (Test-Path $log) { Get-Content $log -Encoding Unicode | Where-Object { $_ -match " error | warning " } | Select-Object -First 20 }
            }
        }
    }
    if ($failed -gt 0) { throw "compilation errors (see above)" }
}

function Invoke-Deploy {
    $started = Get-Date
    $data = Resolve-DataDir
    $install = Resolve-InstallDir $data
    $common = Resolve-CommonDir
    Step-Generate
    Step-Tests
    Step-Fixtures
    Step-Install $data $common
    Step-Compile $data $install
    Log ("done in {0:N0}s. Reload in MT5: Navigator -> right click -> Refresh." -f ((Get-Date) - $started).TotalSeconds)
}

function Get-Fingerprint {
    $paths = @(
        (Join-Path $Mt5 "MQL5\Include"), (Join-Path $Mt5 "MQL5\Experts"), (Join-Path $Mt5 "MQL5\Scripts"),
        (Join-Path $Mt5 "MQL5\Presets"), (Join-Path $Mt5 "tools"),
        (Join-Path $Repo "strategies\orb_intraday\config.py"), (Join-Path $Repo "bot\risk.py"),
        (Join-Path $Repo "bot\account_guard.py"), (Join-Path $Repo "bot\orb_config.py")
    )
    (Get-ChildItem $paths -Recurse -File -Include *.mq5, *.mqh, *.set, *.py, *.ps1, *.sh -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notin @("Params.mqh", "GeneratedCore.mqh") -and $_.FullName -notlike "*__pycache__*" } |
        ForEach-Object { "$($_.FullName)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }) -join "`n"
}

if (-not $Watch) { Invoke-Deploy; exit 0 }

Log "watch mode: deploying now, then on every change (poll ${WatchIntervalSeconds}s, Ctrl+C to stop)"
try { Invoke-Deploy } catch { Log "deploy failed: $_" }
$last = Get-Fingerprint
while ($true) {
    Start-Sleep -Seconds $WatchIntervalSeconds
    $current = Get-Fingerprint
    if ($current -ne $last) {
        Log "change detected -> redeploy"
        try { Invoke-Deploy } catch { Log "deploy failed -- fix and save again: $_" }
        $last = Get-Fingerprint
    }
}
