function Update-CutroomToml([string]$Text, [string]$Block = "") {
    $runtime = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
    $helper = Join-Path $PSScriptRoot "codex_toml.py"
    if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
        throw "CUTROOM Python environment is required to safely edit TOML."
    }
    $payload = @{ text = $Text; block = $Block } | ConvertTo-Json -Compress
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($payload))
    $result = $encoded | & $runtime $helper
    if ($LASTEXITCODE -ne 0) { throw "Codex TOML could not be safely edited; configuration was not changed." }
    return [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$result))
}
