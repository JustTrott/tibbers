<# :
@echo off
title tibbers party check
powershell -NoProfile -ExecutionPolicy Bypass -Command "iex (${%~f0} | Out-String)"
echo.
pause
exit /b
#>

# ---------------------------------------------------------------------------
#  tibbers party check
#
#  Prints the room id that tibbers 1.2.0 would use for your current League
#  party. Everyone in the same party should see the same room.
#
#  Reads only the League client on this machine. Sends nothing anywhere.
#
#  The request goes through Windows' own curl.exe: the League client speaks
#  TLS 1.3, which Windows PowerShell 5 cannot.
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Stop'

function Sha([string]$s) {
  -join ([Security.Cryptography.SHA256]::Create().ComputeHash(
    [Text.Encoding]::UTF8.GetBytes($s)) | ForEach-Object { $_.ToString('x2') })
}

function Note([string]$m, [string]$c) { Write-Host ('  ' + $m) -ForegroundColor $c }

Write-Host ''
Note 'tibbers party check' 'Cyan'
Write-Host ''

$curl = Join-Path $env:SystemRoot 'System32\curl.exe'
if (-not (Test-Path $curl)) {
  Note 'This needs curl.exe, which ships with Windows 10 and 11.' 'Yellow'
  Note 'Windows Update should bring it. Nothing else to do here.' 'Yellow'
  return
}

$roots = @(
  'C:\Riot Games\League of Legends\lockfile',
  (Join-Path $env:ProgramFiles 'Riot Games\League of Legends\lockfile')
)
$lfp = $roots | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $lfp) {
  Note 'Could not find a running League client.' 'Yellow'
  Note 'Start League, join the party, then run this again.' 'Gray'
  return
}

$lf  = (Get-Content $lfp) -split ':'
$url = 'https://127.0.0.1:' + $lf[2] + '/lol-lobby/v1/parties/player'
$raw = (& $curl -s -k -u ('riot:' + $lf[3]) $url) -join ''

if ($LASTEXITCODE -ne 0 -or -not $raw) {
  Note 'The League client did not answer.' 'Yellow'
  Note 'It has to be fully started, not just launching.' 'Gray'
  return
}

try { $p = $raw | ConvertFrom-Json } catch {
  Note 'The client answered with something unexpected.' 'Yellow'
  return
}

$party = $p.currentParty
if (-not $party -or -not $party.partyId) {
  Note 'No party right now.' 'Yellow'
  Note 'Invite someone, or accept an invite, then run this again.' 'Gray'
  return
}

$room = (Sha ('tibbers-party-1' + $party.partyId)).Substring(0, 32)
$me   = (Sha ($room + $p.puuid)).Substring(0, 16)

Write-Host ('  party of ' + @($party.players).Count + ' player(s)')
Write-Host ('  room     ' + $room.Substring(0, 12)) -ForegroundColor Green
Write-Host ('  you      ' + $me.Substring(0, 10)) -ForegroundColor DarkGray
Write-Host ''
Note 'Everyone in the party should see the SAME room.' 'Gray'
Note 'The -you- line is your own and is meant to differ.' 'Gray'
