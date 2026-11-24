# Prepares PS5SX3 on the console to take its updates from this repository (run once, PS5 on, PS5SX3 closed).
#   1. keeps the developer's installer as PS5SX3-Installer-stock.elf
#   2. puts the fetcher (console\PS5SX3-Installer.elf, built by the "PS5SX3 + The Bigs 2 fix" workflow, ps5sx3.yml) in its place
#   3. writes the feed address to rpcs3\config\update-url.txt and /data/PS5SX3-Installer/manifest-url.txt
# Undo: .\setup_ps5.ps1 -Undo
param([switch]$Undo)
$ErrorActionPreference = 'Stop'
$Base = "http://192.168.1.222:8888"
$Title = "/data/homebrew/PPSA99303"
# Everything updates from github.com/Sypherx316/rpcs3: its ps5sx3.yml workflow builds the package and writes this notice
$Feed = "https://raw.githubusercontent.com/Sypherx316/rpcs3/master/ps5sx3/latest.json"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Upload = Join-Path $Here "..\scripts\ps5upload.ps1"

function List($path) {
    (Invoke-RestMethod -Method Post -Uri ("$Base/api/list?path=" + [uri]::EscapeDataString($path)) -TimeoutSec 20).entries
}
function Rename($path, $name) {
    $r = Invoke-RestMethod -Method Post -TimeoutSec 30 -Uri ("$Base/api/rename?path=" + [uri]::EscapeDataString($path) + "&name=" + [uri]::EscapeDataString($name))
    if (-not $r.ok) { throw "rename of $path failed" }
}
function Sha($path) {
    $tmp = [IO.Path]::GetTempFileName()
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -OutFile $tmp -Uri ("$Base/fs?path=" + [uri]::EscapeDataString($path))
    $h = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower(); Remove-Item $tmp; $h
}

$names = (List $Title).name
$stockLocal = Join-Path $Here "console\PS5SX3-Installer-stock.elf"
$fetcherLocal = Join-Path $Here "console\PS5SX3-Installer.elf"

if ($Undo) {
    if ($names -notcontains "PS5SX3-Installer-stock.elf") { "Nothing to undo: there is no PS5SX3-Installer-stock.elf"; return }
    Rename "$Title/PS5SX3-Installer.elf" "PS5SX3-Installer.tb2-fetch.elf"
    Rename "$Title/PS5SX3-Installer-stock.elf" "PS5SX3-Installer.elf"
    $tmp = Join-Path $env:TEMP "update-url.txt"; Set-Content -Path $tmp -Value "" -NoNewline -Encoding ascii
    & $Upload -Dest "$Title/rpcs3/config" -Sources $tmp -Overwrite | Out-Null
    "Undone: the developer's installer is back and update-url.txt is empty (PS5SX3 reads the developer's updates again)."
    return
}

foreach ($f in $stockLocal, $fetcherLocal) { if (-not (Test-Path $f)) { throw "missing $f" } }
$fetcherSha = (Get-FileHash $fetcherLocal -Algorithm SHA256).Hash.ToLower()

# 1. the developer's installer, kept beside it
if ($names -notcontains "PS5SX3-Installer-stock.elf") {
    $current = Sha "$Title/PS5SX3-Installer.elf"
    if ($current -eq $fetcherSha) { throw "PS5SX3-Installer.elf is already the fetcher but there is no -stock.elf: put it back by hand" }
    Rename "$Title/PS5SX3-Installer.elf" "PS5SX3-Installer-stock.elf"
    "1. the developer's installer is now PS5SX3-Installer-stock.elf (sha256 $current)"
} else { "1. PS5SX3-Installer-stock.elf is already there" }

# 2. the fetcher
& $Upload -Dest $Title -Sources $fetcherLocal -Overwrite | Out-Null
if ((Sha "$Title/PS5SX3-Installer.elf") -ne $fetcherSha) { throw "the fetcher did not arrive intact" }
"2. PS5SX3-Installer.elf is the fetcher (sha256 $fetcherSha, checked on the console)"

# 3. the feed address
$tmpDir = Join-Path $env:TEMP "ps5sx3-setup"; New-Item -ItemType Directory -Force $tmpDir | Out-Null
$u = Join-Path $tmpDir "update-url.txt"; Set-Content -Path $u -Value $Feed -NoNewline -Encoding ascii
$m = Join-Path $tmpDir "manifest-url.txt"; Set-Content -Path $m -Value $Feed -NoNewline -Encoding ascii
& $Upload -Dest "$Title/rpcs3/config" -Sources $u -Overwrite | Out-Null
& $Upload -Dest "/data/PS5SX3-Installer" -Sources $m -Overwrite | Out-Null
"3. feed: $Feed"
"Done. Open PS5SX3: it now checks this repository for updates."
