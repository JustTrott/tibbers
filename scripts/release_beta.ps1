# Cut a beta of the version this branch is building, for testers.
#
# Normally nobody runs this: .github/workflows/beta.yml runs it with -Publish
# on every push to a version branch. It is here to be run by hand too, and
# without -Publish it is a dry run.
#
# A beta is a GitHub pre-release tagged vX.Y.Z-beta.N, where X.Y.Z is the
# branch. Stable installs never see it (they ask releases/latest, which skips
# pre-releases); a beta install updates itself to the next beta of X.Y.Z, and
# to X.Y.Z once that ships -- see tibbers/update.py. Testers join by running a
# beta's setup once.
#
# The version is stamped into the build only: __init__.py is put back
# afterwards and nothing is committed, so the branch carries no bot commits.
# N is one past the highest beta of X.Y.Z ever tagged on GitHub, so a number
# is never reused even when its release was deleted.
#
#   powershell -ExecutionPolicy Bypass -File scripts\release_beta.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\release_beta.ps1 -Publish

param(
    [switch]$Publish,
    # The version branch. CI checks out a detached commit, so it passes this.
    [string]$Branch = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Branch) { $Branch = (git rev-parse --abbrev-ref HEAD).Trim() }
if ($Branch -notmatch '^\d+\.\d+\.\d+$') {
    throw "on '$Branch': betas are cut from a version branch such as 1.2.0"
}
if (git status --porcelain --untracked-files=no) {
    throw "the working tree has uncommitted changes; commit or stash them first"
}

git fetch --quiet --tags origin
$sha = (git rev-parse HEAD).Trim()
if ($Publish) {
    # A beta names a commit testers can find on GitHub. It need not be the
    # tip: a queued CI run publishes its own push even if another followed.
    git merge-base --is-ancestor $sha "origin/$Branch"
    if ($LASTEXITCODE -ne 0) { throw "HEAD is not on origin/$Branch; push first" }
}

$betas = @(git tag --list "v$Branch-beta.*" |
    ForEach-Object { if ($_ -match '-beta\.(\d+)$') { [int]$Matches[1] } })
$last = ($betas + 0 | Measure-Object -Maximum).Maximum
$version = "$Branch-beta.$($last + 1)"
$tag = "v$version"
Write-Host "==> Cutting $tag from $Branch at $($sha.Substring(0, 9))"

$init = Join-Path $root "tibbers\__init__.py"
(Get-Content $init -Raw) -replace '__version__ = "[^"]*"', "__version__ = `"$version`"" |
    Set-Content -NoNewline $init

try {
    & (Join-Path $PSScriptRoot "build_windows.ps1") -Installer
    $setup = Join-Path $root "dist\Tibbers-windows-setup.exe"
    if (-not (Test-Path $setup)) { throw "$setup was not built" }

    if (-not $Publish) {
        Write-Host ""
        Write-Host "Built $setup as $version. Not published (dry run)."
        return
    }

    # The notes: what changed since the last beta, then this version's
    # CHANGELOG section.
    $since = if ($last -gt 0) { "v$Branch-beta.$last" } else { "origin/main" }
    $changes = (git log --no-merges --format="- %s" "$since..$sha") -join "`n"
    $log = Get-Content (Join-Path $root "CHANGELOG.md") -Raw
    $section = ""
    if ($log -match "(?ms)^## $([regex]::Escape($Branch))\b[^\n]*\n(.*?)(?=^## |\z)") {
        $section = $Matches[1].Trim()
    }
    $notes = Join-Path ([IO.Path]::GetTempPath()) "tibbers-$tag-notes.md"
    @"
**Beta, for testing.** Run the setup below once. It then updates itself to each new beta of $Branch, and to $Branch when that ships -- after which it is an ordinary install again.

### Since $since
$changes

### $Branch so far
$section
"@ | ForEach-Object {
        # Windows PowerShell's UTF8 writes a byte-order mark, which GitHub
        # keeps at the top of the notes; write it without one.
        [IO.File]::WriteAllText($notes, $_, [Text.UTF8Encoding]::new($false))
    }

    gh release create $tag $setup --prerelease --target $sha `
        --title "tibbers $version" --notes-file $notes
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
    Write-Host "==> Published $tag"
    Write-Host "    https://github.com/JustTrott/tibbers/releases/tag/$tag"
}
finally {
    git checkout --quiet -- $init
}
