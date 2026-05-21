param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }),
    [string]$Marketplace,
    [string]$InstallLabel,
    [string]$TargetRoot,
    [switch]$MarketplaceSource
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$source = Resolve-Path -LiteralPath (Join-Path $repoRoot "plugins\review-suite")
$cacheRoot = Join-Path $CodexHome "plugins\cache"
$marketplacesRoot = Join-Path $CodexHome ".tmp\marketplaces"

function Test-ReviewSuiteRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $manifestPath = Join-Path $Path ".codex-plugin\plugin.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        return $false
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    return [string]$manifest.name -eq "review-suite"
}

function Test-ReviewSuiteRepoRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    return Test-ReviewSuiteRoot -Path (Join-Path $Path "plugins\review-suite")
}

function Test-IsInsidePath {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    $resolvedChild = (Resolve-Path -LiteralPath $Child).Path.TrimEnd("\", "/")
    $resolvedParent = (Resolve-Path -LiteralPath $Parent).Path.TrimEnd("\", "/")
    return $resolvedChild.Equals($resolvedParent, [System.StringComparison]::OrdinalIgnoreCase) -or
        $resolvedChild.StartsWith("$resolvedParent\", [System.StringComparison]::OrdinalIgnoreCase) -or
        $resolvedChild.StartsWith("$resolvedParent/", [System.StringComparison]::OrdinalIgnoreCase)
}

function Resolve-TargetRoot {
    if ($MarketplaceSource) {
        if ($Marketplace -or $InstallLabel) {
            throw "-MarketplaceSource cannot be combined with -Marketplace or -InstallLabel."
        }
        if ($TargetRoot) {
            return (Resolve-Path -LiteralPath $TargetRoot).Path
        }
        return (Resolve-Path -LiteralPath (Join-Path $marketplacesRoot "review-suite")).Path
    }

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
$resolvedTarget = (Resolve-Path -LiteralPath $target).Path

if ($MarketplaceSource) {
    if (-not (Test-ReviewSuiteRepoRoot -Path $resolvedTarget)) {
        throw "Target is not a review-suite marketplace source clone: $resolvedTarget"
    }
    if (-not (Test-IsInsidePath -Child $resolvedTarget -Parent $marketplacesRoot)) {
        throw "Refusing to sync marketplace source outside Codex marketplaces temp root: $resolvedTarget"
    }
    robocopy $repoRoot.Path $resolvedTarget /MIR /XD .git __pycache__ .pytest_cache .tmp /XF "*.pyc" ".codex-marketplace-install.json" | Out-String | Write-Output
}
else {
    if (-not (Test-ReviewSuiteRoot -Path $resolvedTarget)) {
        throw "Target is not an installed review-suite plugin root: $resolvedTarget"
    }
    if (-not (Test-IsInsidePath -Child $resolvedTarget -Parent $cacheRoot)) {
        throw "Refusing to sync outside Codex plugin cache: $resolvedTarget"
    }
    robocopy $source.Path $resolvedTarget /MIR /XD __pycache__ .pytest_cache /XF "*.pyc" | Out-String | Write-Output
}

$code = $LASTEXITCODE
if ($code -ge 8) {
    throw "robocopy failed with exit code $code"
}

Write-Output "synced: $resolvedTarget"
