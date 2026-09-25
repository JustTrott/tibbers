# Cut a beta of the version this branch is building, for testers.
#
# A beta is a GitHub pre-release tagged vX.Y.Z-beta.N. Stable installs never
# see it (they ask releases/latest, which skips pre-releases); a beta install
# follows every release and updates itself to the next beta, and to X.Y.Z
# once that ships -- see tibbers/update.py. Testers join by running the
# beta's setup by hand.
#
#   powershell -ExecutionPolicy Bypass -File scripts\release_beta.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\release_beta.ps1 -Publish
#
# Without -Publish: stamps the next beta number, builds the installer into
# dist\ and puts __init__.py back, so nothing is committed or published. With
# -Publish: also commits the version on this branch, pushes it, and publishes
# the pre-release with the installer and this version's CHANGELOG section.
#
# N is one past the highest beta of X.Y.Z ever tagged on GitHub, so a number
# is never reused even when its release was deleted.

param(
    [switch]$Publish
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -notmatch '^\d+\.\d+\.\d+$') {
    throw "on '$branch': betas are cut from a version branch such as 1.2.0"
}
if (git status --porcelain --untracked-files=no) {
    throw "the working tree has uncommitted changes; commit or stash them first"
}

git fetch --quiet --tags origin
$tags = git ls-remote --tags origin "refs/tags/v$branch-beta.*" |
    ForEach-Object { if ($_ -match "v$([regex]::Escape($branch))-beta\.(\d+)$") { [int]$Matches[1] } }
$n = 1 + (@($tags) + 0 | Measure-Object -Maximum).Maximum
$version = "$branch-beta.$n"
$tag = "v$version"
Write-Host "==> Cutting $tag from $branch"

$init = Join-Path $root "tibbers\__init__.py"
(Get-Content $init -Raw) -replace '__version__ = "[^"]*"', "__version__ = `"$version`"" |
    Set-Content -NoNewline $init

try {
    & (Join-Path $PSScriptRoot "build_windows.ps1") -Installer
    $setup = Join-Path $root "dist\Tibbers-windows-setup.exe"
    if (-not (Test-Path $setup)) { throw "$setup was not built" }

    if (-not $Publish) {
        Write-Host ""
        Write-Host "Built $setup as $version. Not published: run again with -Publish"
        Write-Host "to commit the version, push $branch and publish $tag as a pre-release."
        return
    }

    # The notes: this version's CHANGELOG section, headed by what a beta is.
    $log = Get-Content (Join-Path $root "CHANGELOG.md") -Raw
    $section = ""
    if ($log -match "(?ms)^## $([regex]::Escape($branch))\b[^\n]*\n(.*?)(?=^## |\z)") {
        $section = $Matches[1].Trim()
    }
    $notes = Join-Path $env:TEMP "tibbers-$tag-notes.md"
    @"
**Beta, for testing.** It updates itself to each new beta, and to $branch when that ships -- after which it is an ordinary install again.

$section
"@ | Set-Content -Encoding UTF8 $notes

    git add $init
    git commit --quiet -m $version
    git push --quiet origin $branch
    $sha = (git rev-parse HEAD).Trim()
    gh release create $tag $setup --prerelease --target $sha `
        --title "tibbers $version" --notes-file $notes
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
    Write-Host "==> Published $tag. Testers install it from:"
    Write-Host "    https://github.com/JustTrott/tibbers/releases/tag/$tag"
}
finally {
    if (-not $Publish) { git checkout --quiet -- $init }
}
