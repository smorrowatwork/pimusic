[CmdletBinding()]
param(
    [string]$Host = "mypizero2",
    [string]$User = "stevenm"
)

$target = "$User@$Host"
$command = "systemctl --user status pimusic-web.service pimusic-mpv.service --no-pager -n 40; printf '\n-- resources --\n'; uptime; free -h; printf '\n-- listening port --\n'; ss -ltn '( sport = :8000 )'"
Write-Host "+ ssh $target `"$command`""
& ssh $target $command
exit $LASTEXITCODE
