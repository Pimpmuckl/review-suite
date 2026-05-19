param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }),
    [string]$Marketplace,
    [string]$InstallLabel,
    [string]$TargetRoot
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$source = Resolve-Path -LiteralPath (Join-Path $repoRoot "plugins\review-suite")
$cacheRoot = Resolve-Path -LiteralPath (Join-Path $CodexHome "plugins\cache")

function Test-ReviewSuiteRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $manifestPath = Join-Path $Path ".codex-plugin\plugin.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        return $false
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    return [string]$manifest.name -eq "review-suite"
}

function Resolve-TargetRoot {
    if ($TargetRoot) {
        return (Resolve-Path -LiteralPath $TargetRoot).Path
    }

    if ($Marketplace -or $InstallLabel) {
        if (-not $Marketplace -or -not $InstallLabel) {
            throw "Pass both -Marketplace and -InstallLabel, or neither."
        }
        return (Resolve-Path -LiteralPath (Join-Path $cacheRoot "$Marketplace\review-suite\$InstallLabel")).Path
    }

    $candidates = @(Get-ChildItem -LiteralPath $cacheRoot -Directory |
        ForEach-Object {
            $marketplaceRoot = Join-Path $_.FullName "review-suite"
            if (Test-Path -LiteralPath $marketplaceRoot) {
                Get-ChildItem -LiteralPath $marketplaceRoot -Directory
            }
        } |
        Where-Object { Test-ReviewSuiteRoot -Path $_.FullName } |
        Select-Object -ExpandProperty FullName)

    if ($candidates.Count -eq 1) {
        return (Resolve-Path -LiteralPath $candidates[0]).Path
    }
    if ($candidates.Count -eq 0) {
        throw "No installed review-suite cache root found under $cacheRoot."
    }
    $choices = $candidates -join "`n"
    throw "Multiple review-suite cache roots found. Pass -TargetRoot or -Marketplace/-InstallLabel.`n$choices"
}

$target = Resolve-TargetRoot
if (-not (Test-ReviewSuiteRoot -Path $target)) {
    throw "Target is not an installed review-suite plugin root: $target"
}

$resolvedTarget = (Resolve-Path -LiteralPath $target).Path
$cacheRootPath = $cacheRoot.Path.TrimEnd("\", "/")
$resolvedTargetPath = $resolvedTarget.TrimEnd("\", "/")
$insideCache = $resolvedTargetPath.Equals($cacheRootPath, [System.StringComparison]::OrdinalIgnoreCase) -or
    $resolvedTargetPath.StartsWith("$cacheRootPath\", [System.StringComparison]::OrdinalIgnoreCase) -or
    $resolvedTargetPath.StartsWith("$cacheRootPath/", [System.StringComparison]::OrdinalIgnoreCase)
if (-not $insideCache) {
    throw "Refusing to sync outside Codex plugin cache: $resolvedTarget"
}

robocopy $source.Path $resolvedTarget /MIR /XD __pycache__ .pytest_cache /XF "*.pyc" | Out-String | Write-Output
$code = $LASTEXITCODE
if ($code -ge 8) {
    throw "robocopy failed with exit code $code"
}

Write-Output "synced: $resolvedTarget"
