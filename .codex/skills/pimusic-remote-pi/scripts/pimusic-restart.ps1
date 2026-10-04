[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern("^pimusic-[a-z0-9-]+\.service$")]
    [string]$Service,
    [string]$Host = "mypizero2",
    [string]$User = "stevenm"
)

$target = "$User@$Host"
$command = "systemctl --user restart $Service && systemctl --user status $Service --no-pager -n 40"
Write-Host "+ ssh $target `"$command`""
& ssh $target $command
exit $LASTEXITCODE
