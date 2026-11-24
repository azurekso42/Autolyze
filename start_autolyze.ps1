# Starts the Autolyze backend (which also serves the frontend) on http://127.0.0.1:8000
# Usage: .\start_autolyze.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# Load .env into the process environment (the backend reads plain env vars).
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Write-Error "No .env found. Run: copy .env.example .env  - then fill it in."
}
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $name, $value = $line.Split("=", 2)
        $value = $value.Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq "'" -and $value[-1] -eq "'") -or ($value[0] -eq '"' -and $value[-1] -eq '"'))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name.Trim(), $value, "Process")
    }
}

# Activate the virtual environment if present.
$activate = Join-Path $root ".venv\Scripts\Activate.ps1"
if (Test-Path $activate) { . $activate }

Set-Location (Join-Path $root "backend")
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
