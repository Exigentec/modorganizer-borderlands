param(
    [Parameter(Mandatory)][string]$Mo2Path
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '..\borderlands' | Resolve-Path
$plugins = Join-Path $Mo2Path 'plugins'
if (-not (Test-Path $plugins)) { throw "No plugins folder at $plugins" }

$dest = Join-Path $plugins 'borderlands'
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item $source $dest -Recurse -Exclude '__pycache__'
Write-Host "Installed to $dest. Restart Mod Organizer 2."
