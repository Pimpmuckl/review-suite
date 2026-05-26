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

    foreach ($name in $marketplaceRootArtifactDirs) {
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

function ConvertTo-MarketplaceRelativePath {
    param([Parameter(Mandatory = $true)][string]$PathText)

    $path = $PathText.Trim().Trim('"').Replace("\", "/")
    if ($path.StartsWith("./")) {
        $path = $path.Substring(2)
    }
    return $path.TrimEnd("/")
}

function Test-MarketplacePathIsKnownArtifact {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $path = ConvertTo-MarketplaceRelativePath -PathText $RelativePath
    if (-not $path) {
        return $false
    }
    if ($path -eq ".codex-marketplace-install.json") {
        return $true
    }
    foreach ($name in $marketplaceRootArtifactDirs) {
        $prefix = "$name/"
        if ($path.Equals($name, [System.StringComparison]::OrdinalIgnoreCase) -or
            $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    foreach ($name in $marketplaceRootArtifactFileNames) {
        if ($path.Equals($name, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    $segments = @($path -split "/")
    foreach ($name in $marketplaceRecursiveArtifactDirs) {
        if ($segments -contains $name) {
            return $true
        }
    }
    $leaf = Split-Path -Leaf $path
    foreach ($glob in $marketplaceRecursiveArtifactFileGlobs) {
        if ($leaf -like $glob) {
            return $true
        }
    }
    return $false
}

function Join-ProcessArguments {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $quoted = foreach ($argument in $Arguments) {
        $value = [string]$argument
        if ($value -notmatch '[\s"]') {
            $value
            continue
        }
        $builder = [System.Text.StringBuilder]::new()
        [void]$builder.Append('"')
        $backslashes = 0
        foreach ($char in $value.ToCharArray()) {
            if ($char -eq '\') {
                $backslashes++
                continue
            }
            if ($char -eq '"') {
                [void]$builder.Append('\' * (($backslashes * 2) + 1))
                [void]$builder.Append('"')
                $backslashes = 0
                continue
            }
            if ($backslashes -gt 0) {
                [void]$builder.Append('\' * $backslashes)
                $backslashes = 0
            }
            [void]$builder.Append($char)
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append('\' * ($backslashes * 2))
        }
        [void]$builder.Append('"')
        $builder.ToString()
    }
    return $quoted -join " "
}

function Get-GitStatusPorcelainEntries {
    param([Parameter(Mandatory = $true)][string]$TargetPath)

    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = "git"
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.CreateNoWindow = $true
    $arguments = @("-C", $TargetPath, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if ($null -ne $processInfo.ArgumentList) {
        foreach ($argument in $arguments) {
            [void]$processInfo.ArgumentList.Add($argument)
        }
    }
    else {
        $processInfo.Arguments = Join-ProcessArguments -Arguments $arguments
    }

    $process = $null
    $stdout = [System.IO.MemoryStream]::new()
    $exitCode = 1

    try {
        $process = [System.Diagnostics.Process]::Start($processInfo)
        if ($null -eq $process) {
            return [pscustomobject]@{ Succeeded = $false; Entries = @() }
        }
        $process.StandardOutput.BaseStream.CopyTo($stdout)
        $process.StandardError.ReadToEnd() | Out-Null
        $process.WaitForExit()
        $exitCode = $process.ExitCode
    }
    catch {
        return [pscustomobject]@{ Succeeded = $false; Entries = @() }
    }
    finally {
        if ($process) {
            $process.Dispose()
        }
    }

    if ($exitCode -ne 0) {
        return [pscustomobject]@{ Succeeded = $false; Entries = @() }
    }

    $segments = @([System.Text.Encoding]::UTF8.GetString($stdout.ToArray()) -split "`0" |
        Where-Object { $_ -ne "" })
    $entries = @()
    for ($index = 0; $index -lt $segments.Count; $index++) {
        $segment = [string]$segments[$index]
        if ($segment.Length -lt 4) {
            $entries += [pscustomobject]@{ StatusCode = ""; Paths = @(); Malformed = $true }
            continue
        }

        $statusCode = $segment.Substring(0, 2).Trim()
        $paths = @($segment.Substring(3))
        if ($statusCode.StartsWith("R") -or $statusCode.StartsWith("C")) {
            if ($index + 1 -ge $segments.Count) {
                $entries += [pscustomobject]@{ StatusCode = $statusCode; Paths = $paths; Malformed = $true }
                continue
            }
            $index++
            $paths += [string]$segments[$index]
        }
        $entries += [pscustomobject]@{ StatusCode = $statusCode; Paths = $paths; Malformed = $false }
    }

    return [pscustomobject]@{ Succeeded = $true; Entries = $entries }
}

function Get-DirectoryFileSnapshot {
    param([Parameter(Mandatory = $true)][string]$RootPath)

    $root = (Resolve-Path -LiteralPath $RootPath).Path.TrimEnd("\", "/")
    $snapshot = @{}
    foreach ($file in Get-ChildItem -LiteralPath $root -File -Recurse -Force -ErrorAction SilentlyContinue) {
        $relative = $file.FullName.Substring($root.Length).TrimStart("\", "/").Replace("\", "/")
        $snapshot[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    return $snapshot
}

function Test-DirectoryMatchesSource {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )

    $sourceSnapshot = Get-DirectoryFileSnapshot -RootPath $SourcePath
    $targetSnapshot = Get-DirectoryFileSnapshot -RootPath $TargetPath
    if ($sourceSnapshot.Count -ne $targetSnapshot.Count) {
        return $false
    }
    foreach ($path in $sourceSnapshot.Keys) {
        if (-not $targetSnapshot.ContainsKey($path)) {
            return $false
        }
        if ($sourceSnapshot[$path] -ne $targetSnapshot[$path]) {
            return $false
        }
    }
    return $true
}

function Test-MarketplacePathMatchesSource {
    param(
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $path = ConvertTo-MarketplaceRelativePath -PathText $RelativePath
    if (-not $path) {
        return $true
    }

    $sourcePath = Join-Path $repoRoot.Path $path
    $targetFilePath = Join-Path $TargetPath $path
    $sourceExists = Test-Path -LiteralPath $sourcePath
    $targetExists = Test-Path -LiteralPath $targetFilePath
    if (-not $sourceExists -and -not $targetExists) {
        return $true
    }
    if ($sourceExists -and $targetExists) {
        $sourceItem = Get-Item -LiteralPath $sourcePath
        $targetItem = Get-Item -LiteralPath $targetFilePath
        if ($sourceItem.PSIsContainer -and $targetItem.PSIsContainer) {
            return Test-DirectoryMatchesSource -SourcePath $sourcePath -TargetPath $targetFilePath
        }
        if (-not $sourceItem.PSIsContainer -and -not $targetItem.PSIsContainer) {
            return (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash -eq
                (Get-FileHash -LiteralPath $targetFilePath -Algorithm SHA256).Hash
        }
    }
    return $false
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

function Get-MarketplaceSourceDefaultSyncBlocker {
    param([Parameter(Mandatory = $true)][string]$TargetPath)

    if (-not (Test-Path -LiteralPath (Join-Path $TargetPath ".git"))) {
        return ""
    }

    $branch = (@(& git -C $TargetPath branch --show-current 2>$null) -join "").Trim()
    if ($LASTEXITCODE -ne 0) {
        return "git status unavailable"
    }
    if (-not $branch) {
        return "git checkout is detached"
    }
    if ($branch -ne "main") {
        return "git branch is $branch, not main"
    }

    $status = Get-GitStatusPorcelainEntries -TargetPath $TargetPath
    if (-not $status.Succeeded) {
        return "git status unavailable"
    }
    $dirty = @($status.Entries | Where-Object {
            $entry = $_
            if ($entry.Malformed) {
                return $true
            }
            $relativePaths = @($entry.Paths | ForEach-Object { ConvertTo-MarketplaceRelativePath -PathText $_ })
            $nonArtifactPaths = @($relativePaths | Where-Object { -not (Test-MarketplacePathIsKnownArtifact -RelativePath $_) })
            if ($nonArtifactPaths.Count -eq 0) {
                return $false
            }
            foreach ($relativePath in $nonArtifactPaths) {
                if (-not (Test-MarketplacePathMatchesSource -TargetPath $TargetPath -RelativePath $relativePath)) {
                    return $true
                }
            }
            return $false
        })
    if ($dirty.Count -gt 0) {
        return "git worktree has local changes"
    }
    return ""
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
            $defaultSyncBlocker = Get-MarketplaceSourceDefaultSyncBlocker -TargetPath $resolvedMarketplaceSourceRoot
            if ($defaultSyncBlocker) {
                Write-Output "skipped marketplace source: $resolvedMarketplaceSourceRoot $defaultSyncBlocker"
            }
            else {
                Sync-MarketplaceSource -TargetPath $resolvedMarketplaceSourceRoot
            }
        }
        else {
            Write-Output "skipped marketplace source: $resolvedMarketplaceSourceRoot is not a review-suite marketplace source clone"
        }
    }
    else {
        Write-Output "skipped marketplace source: $marketplaceSourceRoot not found"
    }
}
