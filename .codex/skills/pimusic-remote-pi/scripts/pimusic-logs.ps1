[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern("^pimusic-[a-z0-9-]+\.service$")]
    [string]$Service,
    [ValidateRange(1, 500)]
    [int]$Lines = 100,
    [string]$TargetHost = "mypizero2",
    [string]$User = "stevenm"
)

$target = "$User@$TargetHost"
$command = "journalctl --user -u $Service --no-pager -n $Lines"
Write-Host "+ ssh $target `"$command`""
& ssh $target $command
exit $LASTEXITCODE
