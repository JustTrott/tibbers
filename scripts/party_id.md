# Does everyone in a party derive the same room id?

The 1.2.0 lobby feature keys its room on a hash of the League party id
(see `LOBBY.md`). This is the one experiment that proves the key
construction, and it needs no tibbers code: everyone in the party runs one
snippet and compares twelve characters.

**Have League open and be in the party together.** Any queue, or just
sitting in a lobby. Everyone should print the same `room`. Your `you`
value is your own and is different for each person by design.

Nothing is sent anywhere. Everything here only reads the local client.

## The easy way: `party_id.bat`

Send people `scripts/party_id.bat`. They double-click it, it prints the
room and waits for a keypress. Nothing to install and nothing to type.

It fetches through Windows' own `curl.exe` rather than PowerShell.
**Windows PowerShell 5.1 cannot talk to the League client at all**: the
client requires TLS 1.3 and .NET Framework does not speak it, so every
request fails with *the underlying connection was closed*. Verified here.
That is only a constraint on hand-written snippets. tibbers itself reads
the client through Python, which is unaffected.

The snippets below are the same thing by hand.

## Windows (PowerShell)

```powershell
$lfp = "C:\Riot Games\League of Legends\lockfile"
if (-not (Test-Path $lfp)) { $lfp = "$env:ProgramFiles\Riot Games\League of Legends\lockfile" }
$lf = (Get-Content $lfp) -split ':'
$hd = @{Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("riot:" + $lf[3]))}
$p = Invoke-RestMethod -SkipCertificateCheck -Headers $hd "https://127.0.0.1:$($lf[2])/lol-lobby/v1/parties/player"
function Sha([string]$s) { -join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s)) | ForEach-Object { $_.ToString('x2') }) }
$room = (Sha ("tibbers-party-1" + $p.currentParty.partyId)).Substring(0,32)
$me   = (Sha ($room + $p.puuid)).Substring(0,16)
"party of $($p.currentParty.players.Count)   room $($room.Substring(0,12))   you $($me.Substring(0,10))"
```

Needs PowerShell 7. Windows PowerShell 5.1 cannot reach the client at
all, for the TLS reason above, so `party_id.bat` uses `curl.exe` instead.

## macOS (Terminal)

```bash
P="/Applications/League of Legends.app/Contents/LoL/lockfile"
[ -f "$P" ] || P="$HOME/Applications/League of Legends.app/Contents/LoL/lockfile"
LF=$(cat "$P")
PORT=$(echo "$LF" | cut -d: -f3); TOK=$(echo "$LF" | cut -d: -f4)
J=$(curl -sk -u "riot:$TOK" "https://127.0.0.1:$PORT/lol-lobby/v1/parties/player")
PID=$(printf '%s' "$J" | grep -o '"partyId":"[^"]*"' | head -1 | cut -d'"' -f4)
ROOM=$(printf 'tibbers-party-1%s' "$PID" | shasum -a 256 | cut -c1-12)
echo "room $ROOM"
```

Only preinstalled tools. Every `partyId` in that response is the same
value, so `head -1` is safe.

## Reading the result

- **Same `room` for everyone** is the expected result, and it means the
  party id is shared, which is the whole assumption 1.2.0 rests on.
- **Different `room` values** means party members see different party ids,
  and the room would have to be keyed on the sorted member puuids instead.
  Everything above that layer is unaffected.
- **Empty or missing `room`** usually means League is not running, or the
  lockfile is somewhere else because the game is installed off the default
  path.

The interesting case to try once the party case works is a **custom
lobby** with people who are not in your party. The plan assumes they all
report the same party id; if they do not, customs fall back to sharing
within your own party only.
