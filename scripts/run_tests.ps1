param([string]$Out = "$PSScriptRoot\runall.txt", [string]$Only = "")
# every test of D:\Biruni-GIS\tests, off screen, one line per test: "name: exit=N"
# (skipped: the load / outside-network / takeover / open-data tests, which need minutes or the internet)
$root = "D:\Biruni-GIS"
$env:QT_QPA_PLATFORM = "offscreen"
$env:QT_QPA_FONTDIR = "C:\Windows\Fonts"   # the layout tests measure text: they need real fonts
$skip = "test_team_load", "test_team_outside", "test_team_takeover", "test_opendata"
Set-Content -Path $Out -Value "" -Encoding utf8
$tests = Get-ChildItem "$root\tests" -Filter "test_*.py" | Where-Object { $skip -notcontains $_.BaseName } | Sort-Object Name
if ($Only) { $tests = $tests | Where-Object { $_.BaseName -match $Only } }
foreach ($t in $tests) {
    $log = Join-Path $env:TEMP ("biruni_" + $t.BaseName + ".log")
    $p = Start-Process -FilePath "$root\engine\bin\python-qgis.bat" -ArgumentList "`"$($t.FullName)`"" -WorkingDirectory $root `
        -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    $null = $p.Handle   # without this the exit code of a finished process is lost
    if (-not $p.WaitForExit(600000)) { try { taskkill /PID $p.Id /T /F | Out-Null } catch {}; $code = "timeout" }
    else { $p.WaitForExit(); $code = $p.ExitCode }
    $tail = ""
    if (Test-Path $log) { $tail = (Get-Content $log -Tail 4) -join " " }
    if ($code -ne 0 -and $tail -match "ALL PASS") { $code = "0 (passed; the process ended with $code while closing)" }
    $fails = @(Select-String -Path $log -Pattern "^FAIL " -ErrorAction SilentlyContinue | Select-Object -First 5 | ForEach-Object { $_.Line })
    Add-Content -Path $Out -Value ("{0}: exit={1}" -f $t.BaseName, $code) -Encoding utf8
    foreach ($f in $fails) { Add-Content -Path $Out -Value ("    " + $f) -Encoding utf8 }
}
Add-Content -Path $Out -Value "DONE" -Encoding utf8
