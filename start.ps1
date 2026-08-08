# Starts the YT->MP3 server.
# First time: edit the line below to set your own password, or set it
# as a permanent environment variable via System Properties > Environment Variables.

if (-not $env:YTMP3_PASSWORD) {
    $env:YTMP3_PASSWORD = Read-Host "Set a password for this server" -AsSecureString | ForEach-Object {
        [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($_))
    }
}

Set-Location $PSScriptRoot
python app.py
