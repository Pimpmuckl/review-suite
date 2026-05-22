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
$marketplaceSourceRoot = Join-Path $marketplacesRoot "review-suite"
$marketplaceRootArtifactDirs = @(".ask-pro", ".review-suite", ".tmp", "tmp", "state", ".venv")
$marketplaceRootArtifactFileNames = @(".coverage", ".env", ".DS_Store", "Thumbs.db", "task_plan.md", "findings.md", "progress.md")
$marketplaceRecursiveArtifactDirs = @("__pycache__", ".pytest_cache", ".ruff_cache")
$marketplaceRecursiveArtifactFileGlobs = @("*.pyc")
$marketplacePreservedDirs = @(".git")
$marketplacePreservedFiles = @(".codex-marketplace-install.json")

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

function Resolve-InstalledTargetRoot {
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

function Resolve-MarketplaceSourceRoot {
    if ($TargetRoot) {
        return (Resolve-Path -LiteralPath $TargetRoot).Path
    }
    return (Resolve-Path -LiteralPath $marketplaceSourceRoot).Path
}

function Invoke-CheckedRobocopy {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [string[]]$ExcludeDirs = @(),
        [string[]]$ExcludeFiles = @()
    )

    $args = @($SourcePath, $TargetPath, "/MIR")
    if ($ExcludeDirs.Count -gt 0) {
        $args += "/XD"
        $args += $ExcludeDirs
    }
    if ($ExcludeFiles.Count -gt 0) {
        $args += "/XF"
        $args += $ExcludeFiles
    }

    & robocopy @args | Out-String | Write-Output
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        throw "robocopy failed with exit code $code"
    }
}

function Remove-PathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not (Test-IsInsidePath -Child $resolved -Parent $Parent)) {
        throw "Refusing to remove path outside marketplace source: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Remove-MarketplaceLocalArtifacts {
    param([Parameter(Mandatory = $true)][string]$TargetPath)

    foreach ($name in ($marketplaceRootArtifactDirs + $marketplaceRecursiveArtifactDirs)) {
        Remove-PathInside -Path (Join-Path $TargetPath $name) -Parent $TargetPath
    }

    foreach ($name in $marketplaceRootArtifactFileNames) {
        Remove-PathInside -Path (Join-Path $TargetPath $name) -Parent $TargetPath
    }

    $artifactDirs = @(Get-ChildItem -LiteralPath $TargetPath -Directory -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in $marketplaceRecursiveArtifactDirs } |
        Sort-Object FullName -Descending)
    foreach ($dir in $artifactDirs) {
        Remove-PathInside -Path $dir.FullName -Parent $TargetPath
    }

    $artifactFiles = @(Get-ChildItem -LiteralPath $TargetPath -File -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $name = $_.Name
            $marketplaceRecursiveArtifactFileGlobs | Where-Object { $name -like $_ }
        })
    foreach ($file in $artifactFiles) {
        Remove-PathInside -Path $file.FullName -Parent $TargetPath
    }
}

function Sync-MarketplaceSource {
    param([Parameter(Mandatory = $true)][string]$TargetPath)

    $resolvedTarget = (Resolve-Path -LiteralPath $TargetPath).Path
    if (-not (Test-ReviewSuiteRepoRoot -Path $resolvedTarget)) {
        throw "Target is not a review-suite marketplace source clone: $resolvedTarget"
    }
    if (-not (Test-IsInsidePath -Child $resolvedTarget -Parent $marketplacesRoot)) {
        throw "Refusing to sync marketplace source outside Codex marketplaces temp root: $resolvedTarget"
    }
    $rootArtifactSourceDirs = $marketplaceRootArtifactDirs | ForEach-Object { Join-Path $repoRoot.Path $_ }
    $rootArtifactSourceFiles = $marketplaceRootArtifactFileNames | ForEach-Object { Join-Path $repoRoot.Path $_ }
    Invoke-CheckedRobocopy `
        -SourcePath $repoRoot.Path `
        -TargetPath $resolvedTarget `
        -ExcludeDirs ($marketplacePreservedDirs + $marketplaceRecursiveArtifactDirs + $rootArtifactSourceDirs) `
        -ExcludeFiles ($marketplaceRecursiveArtifactFileGlobs + $marketplacePreservedFiles + $rootArtifactSourceFiles)
    Remove-MarketplaceLocalArtifacts -TargetPath $resolvedTarget
    Write-Output "synced marketplace source: $resolvedTarget"
}

function Sync-InstalledCache {
    param([Parameter(Mandatory = $true)][string]$TargetPath)

    $resolvedTarget = (Resolve-Path -LiteralPath $TargetPath).Path
    if (-not (Test-ReviewSuiteRoot -Path $resolvedTarget)) {
        throw "Target is not an installed review-suite plugin root: $resolvedTarget"
    }
    if (-not (Test-IsInsidePath -Child $resolvedTarget -Parent $cacheRoot)) {
        throw "Refusing to sync outside Codex plugin cache: $resolvedTarget"
    }
    Invoke-CheckedRobocopy `
        -SourcePath $source.Path `
        -TargetPath $resolvedTarget `
        -ExcludeDirs @("__pycache__", ".pytest_cache", ".ruff_cache") `
        -ExcludeFiles @("*.pyc")
    Write-Output "synced installed cache: $resolvedTarget"
}

if ($MarketplaceSource) {
    if ($Marketplace -or $InstallLabel) {
        throw "-MarketplaceSource cannot be combined with -Marketplace or -InstallLabel."
    }
    Sync-MarketplaceSource -TargetPath (Resolve-MarketplaceSourceRoot)
    return
}

Sync-InstalledCache -TargetPath (Resolve-InstalledTargetRoot)

if (-not $TargetRoot -and -not $Marketplace -and -not $InstallLabel) {
    if (Test-Path -LiteralPath $marketplaceSourceRoot) {
        $resolvedMarketplaceSourceRoot = Resolve-MarketplaceSourceRoot
        if (Test-ReviewSuiteRepoRoot -Path $resolvedMarketplaceSourceRoot) {
            Sync-MarketplaceSource -TargetPath $resolvedMarketplaceSourceRoot
        }
        else {
            Write-Output "skipped marketplace source: $resolvedMarketplaceSourceRoot is not a review-suite marketplace source clone"
        }
    }
    else {
        Write-Output "skipped marketplace source: $marketplaceSourceRoot not found"
    }
}
