[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string[]]$File,
    [switch]$AllowOverwrite,
    [string]$TargetHost = "mypizero2",
    [string]$User = "stevenm",
    [string]$RemoteRoot = "/home/stevenm/pimusic"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$target = "$User@$TargetHost"
$excludedSegments = @(".git", ".venv", "venv", "__pycache__", ".vscode", ".codex", ".agents")
$excludedExtensions = @(".pyc", ".pyo", ".pem", ".key")

function Quote-Posix([string]$Value) {
    return "'" + $Value.Replace("'", "'`"'`"'") + "'"
}

foreach ($relativePath in $File) {
    if ([IO.Path]::IsPathRooted($relativePath) -or $relativePath -match "(^|[\\/])\.\.([\\/]|$)") {
        throw "Use a repository-relative file path without '..': $relativePath"
    }
    if ($relativePath -notmatch "^[A-Za-z0-9][A-Za-z0-9._/-]*$") {
        throw "Use a simple repository-relative file path (letters, numbers, '.', '_', '-', '/'): $relativePath"
    }

    $segments = $relativePath -split "[\\/]"
    if ($segments | Where-Object { $_ -in $excludedSegments }) {
        throw "Development-only path is excluded from deployment: $relativePath"
    }

    $localPath = Join-Path $repositoryRoot $relativePath
    if (-not (Test-Path -LiteralPath $localPath -PathType Leaf)) {
        throw "Only existing regular files may be deployed: $relativePath"
    }
    if ([IO.Path]::GetExtension($localPath) -in $excludedExtensions -or (Split-Path $localPath -Leaf) -match "^\.env(\.|$)") {
        throw "Secret, cache, or generated file is excluded from deployment: $relativePath"
    }

    $normalizedRelative = ($relativePath -replace "\\", "/")
    $remotePath = "$RemoteRoot/$normalizedRelative"
    $remoteDirectory = (Split-Path $remotePath -Parent) -replace "\\", "/"
    $localHash = (Get-FileHash -LiteralPath $localPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $remoteHashOutput = & ssh $target "if test -f $(Quote-Posix $remotePath); then sha256sum $(Quote-Posix $remotePath) | cut -d ' ' -f1; fi"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect remote file: $remotePath"
    }
    $remoteHash = if ($null -eq $remoteHashOutput) { "" } else { $remoteHashOutput.Trim().ToLowerInvariant() }

    if ($remoteHash -and $remoteHash -ne $localHash -and -not $AllowOverwrite) {
        throw "Remote file differs and was not overwritten: $remotePath. Inspect it, then rerun with -AllowOverwrite only if replacing it is intended."
    }

    Write-Host "+ ssh $target `"mkdir -p $remoteDirectory`""
    & ssh $target "mkdir -p $(Quote-Posix $remoteDirectory)"
    if ($LASTEXITCODE -ne 0) { throw "Unable to create remote directory: $remoteDirectory" }

    Write-Host "+ scp $localPath ${target}:$remotePath"
    & scp $localPath "${target}:$remotePath"
    if ($LASTEXITCODE -ne 0) { throw "Copy failed: $relativePath" }
}
