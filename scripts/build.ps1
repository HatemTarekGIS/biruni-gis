<#
.SYNOPSIS
  Builds Biruni GIS.

.DESCRIPTION
  -Dev      : compile the launcher into build\ and point it at the working tree
              (engine\ + app\) for development runs. Fast.
  (default) : full release - fresh engine from the QGIS MSI (minus components we
              may not redistribute), app payload, launcher, then the Inno Setup
              installer in dist\.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -Dev
  powershell -ExecutionPolicy Bypass -File scripts\build.ps1
#>
param(
    [switch]$Dev,
    [string]$Msi = "",
    [string]$Iscc = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$App = Join-Path $Root "app"
$Icon = Join-Path $App "plugins\biruni\resources\branding\biruni.ico"

# ---- version (single source: app\plugins\biruni\version.py) ----------------
$verLine = Select-String -Path (Join-Path $App "plugins\biruni\version.py") -Pattern '^VERSION\s*=\s*"([^"]+)"'
$Version = $verLine.Matches[0].Groups[1].Value
Write-Host "Biruni GIS $Version"

function Build-Launcher([string]$OutExe) {
    $fw = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319"
    $parts = $Version.Split(".") + @("0", "0", "0")
    $v4 = ($parts[0..3] -join ".")
    $info = @"
[assembly: System.Reflection.AssemblyTitle("Biruni GIS")]
[assembly: System.Reflection.AssemblyProduct("Biruni GIS")]
[assembly: System.Reflection.AssemblyDescription("Biruni GIS - free and open-source desktop GIS")]
[assembly: System.Reflection.AssemblyCompany("Biruni GIS contributors")]
[assembly: System.Reflection.AssemblyCopyright("GNU GPL v2 or later")]
[assembly: System.Reflection.AssemblyVersion("$v4")]
[assembly: System.Reflection.AssemblyFileVersion("$v4")]
[assembly: System.Reflection.AssemblyInformationalVersion("$Version")]
"@
    $tmp = Join-Path $env:TEMP "biruni-versioninfo.cs"
    [System.IO.File]::WriteAllText($tmp, $info)
    New-Item -ItemType Directory -Force (Split-Path $OutExe) | Out-Null
    & "$fw\csc.exe" /nologo /target:winexe /optimize+ "/out:$OutExe" "/win32icon:$Icon" `
        "/win32manifest:$Root\launcher\app.manifest" "/r:$fw\System.Windows.Forms.dll" "/r:$fw\System.Drawing.dll" `
        "$Root\launcher\Launcher.cs" $tmp
    if ($LASTEXITCODE -ne 0) { throw "launcher compilation failed" }
    Remove-Item $tmp
}

if ($Dev) {
    $exe = Join-Path $Root "build\Biruni GIS.exe"
    Build-Launcher $exe
    "engine=$Root\engine`r`napp=$App" | Set-Content -Encoding ascii (Join-Path $Root "build\biruni-dev.ini")
    Write-Host "dev launcher: $exe"
    return
}

# ---- release ----------------------------------------------------------------
$Dist = Join-Path $Root "dist"
$Stage = Join-Path $Dist "stage"
if (-not $Msi) {
    $Msi = Get-ChildItem (Join-Path $Root "_downloads") -Filter "QGIS-OSGeo4W-*.msi" |
        Sort-Object Name | Select-Object -Last 1 -ExpandProperty FullName
}
if (-not $Msi -or -not (Test-Path $Msi)) { throw "QGIS MSI not found (use -Msi)" }
if (-not $Iscc) { $Iscc = Join-Path $Root "_tools\InnoSetup\ISCC.exe" }
if (-not (Test-Path $Iscc)) { throw "Inno Setup compiler not found: $Iscc" }

if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force $Stage | Out-Null

Write-Host "1/5 extracting engine from $(Split-Path -Leaf $Msi)"
$msiOut = Join-Path $Dist "_msi"
if (Test-Path $msiOut) { Remove-Item $msiOut -Recurse -Force }
$p = Start-Process msiexec.exe -ArgumentList "/a", "`"$Msi`"", "/qn", "TARGETDIR=`"$msiOut`"" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "MSI extraction failed ($($p.ExitCode))" }
$engineSrc = Get-ChildItem $msiOut -Directory | Where-Object Name -like "QGIS*" | Select-Object -First 1
Move-Item $engineSrc.FullName (Join-Path $Stage "engine")
Remove-Item $msiOut -Recurse -Force

Write-Host "2/5 removing components that may not be redistributed"
Get-Content (Join-Path $PSScriptRoot "engine-exclude.txt") | Where-Object { $_ -and -not $_.StartsWith("#") } | ForEach-Object {
    $f = Join-Path $Stage ("engine\" + ($_ -replace "/", "\"))
    if (Test-Path $f) { Remove-Item $f -Force } else { Write-Warning "not found: $_" }
}
Remove-Item (Join-Path $Stage "engine\include") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "3/5 copying the Biruni app payload"
robocopy $App (Join-Path $Stage "app") /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
Copy-Item (Join-Path $Root "LICENSE") (Join-Path $Stage "LICENSE.txt")
Copy-Item (Join-Path $Root "NOTICE.txt") (Join-Path $Stage "NOTICE.txt")

Write-Host "4/5 building the launcher"
Build-Launcher (Join-Path $Stage "Biruni GIS.exe")

Write-Host "5/5 building the installer (this takes a while)"
& $Iscc /Q "/DAppVersion=$Version" "/DStageDir=$Stage" "/DOutDir=$Dist" (Join-Path $Root "installer\biruni.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
$setup = Join-Path $Dist "Biruni-GIS-$Version-Setup.exe"

# ---- optional code signing --------------------------------------------------
# Set BIRUNI_SIGN_CMD to a command that signs one file passed as {file}, e.g.
#   signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a "{file}"
# (SignPath Foundation signs open-source releases in CI instead; see docs\RELEASING.md.)
if ($env:BIRUNI_SIGN_CMD) {
    Write-Host "signing the installer"
    $cmd = $env:BIRUNI_SIGN_CMD.Replace("{file}", $setup)
    cmd /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "signing failed ($LASTEXITCODE)" }
}
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash
Set-Content -Encoding ascii -Path "$setup.sha256" -Value "$hash  $(Split-Path -Leaf $setup)"
Write-Host ("done: {0} ({1:N0} MB)  SHA256 {2}" -f $setup, ((Get-Item $setup).Length / 1MB), $hash)
