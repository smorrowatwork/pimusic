[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Command,
    [string]$Host = "mypizero2",
    [string]$User = "stevenm"
)

$target = "$User@$Host"
Write-Host "+ ssh $target `"$Command`""
& ssh $target $Command
exit $LASTEXITCODE
