$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if ($args.Count -gt 0) {
    python -m core.app @args
} else {
    python -m launcher.app
}
exit $LASTEXITCODE
