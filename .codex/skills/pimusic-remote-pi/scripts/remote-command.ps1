[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Command,
    [string]$TargetHost = "mypizero2",
    [string]$User = "stevenm"
)

$target = "$User@$TargetHost"
Write-Host "+ ssh $target `"$Command`""
& ssh $target $Command
exit $LASTEXITCODE
