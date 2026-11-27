# Persists CallIntel's recommended Redis settings in Memurai's config file and restarts the service.
# Needs admin rights (the file lives in Program Files). Run from an elevated prompt, or:
#   Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList '-NoProfile -File "<path>\memurai_persist_settings.ps1"'
#
#   appendonly yes                -> work queue, DLQ, manifest and learned aliases survive restarts
#   maxmemory 2gb                 -> hard memory cap (Memurai Developer may use at most 50% of RAM anyway)
#   maxmemory-policy volatile-lru -> only keys with a TTL (LLM / embedding caches) are ever evicted

$ErrorActionPreference = 'Stop'
$conf   = 'C:\Program Files\Memurai\memurai.conf'
$backup = "$conf.callintel.bak"
$log    = 'C:\Temp\memurai\persist.log'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

function Write-Log([string]$msg) { "$(Get-Date -Format HH:mm:ss) $msg" | Tee-Object -FilePath $log -Append }

try {
    if (-not (Test-Path $backup)) { Copy-Item $conf $backup; Write-Log "backup written: $backup" }
    $lines = [System.Collections.Generic.List[string]](Get-Content $conf)
    $want = [ordered]@{ 'appendonly' = 'yes'; 'maxmemory' = '2gb'; 'maxmemory-policy' = 'volatile-lru' }
    foreach ($key in $want.Keys) {
        $pattern = '^\s*' + [regex]::Escape($key) + '\s'
        $hits = @(for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match $pattern) { $i } })
        if ($hits.Count) { foreach ($i in $hits) { $lines[$i] = "$key $($want[$key])" }; Write-Log "set $key $($want[$key]) (line $($hits -join ','))" }
        else { $lines.Add("$key $($want[$key])"); Write-Log "added $key $($want[$key])" }
    }
    [System.IO.File]::WriteAllLines($conf, $lines)   # UTF-8 without BOM, like the original file
    Restart-Service -Name Memurai
    Start-Sleep -Seconds 2
    Write-Log "service: $((Get-Service Memurai).Status)"
    Write-Log "DONE"
} catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
