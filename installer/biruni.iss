; Biruni GIS - Windows installer (Inno Setup 6)
; Built by scripts\build.ps1, which passes /DAppVersion=... /DStageDir=... /DOutDir=...

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\dist\stage"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif
#define AppName "Biruni GIS"
#define AppExe "Biruni GIS.exe"

[Setup]
AppId={{6B1C2F57-9A4E-4D8B-B3E1-0F5A7C2D9E41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Biruni GIS contributors
AppCopyright=Copyright (C) 2026 Biruni GIS contributors - GNU GPL v2 or later
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#StageDir}\LICENSE.txt
InfoBeforeFile={#StageDir}\NOTICE.txt
SetupIconFile={#StageDir}\app\plugins\biruni\resources\branding\biruni.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
OutputDir={#OutDir}
OutputBaseFilename=Biruni-GIS-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=8
ChangesAssociations=yes
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"

[CustomMessages]
english.PreparingEngine=Preparing the map engine (this takes a moment)...
arabic.PreparingEngine=جارٍ تجهيز محرك الخرائط (قد يستغرق ذلك لحظات)...
english.ProjectFile=QGIS project
arabic.ProjectFile=مشروع QGIS

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppName}"; Tasks: desktopicon

[Registry]
; Offer Biruni GIS in "Open with" for QGIS projects without taking over the default app.
Root: HKA; Subkey: "Software\Classes\BiruniGIS.Project"; ValueType: string; ValueName: ""; ValueData: "{cm:ProjectFile}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\BiruniGIS.Project\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"",0"
Root: HKA; Subkey: "Software\Classes\BiruniGIS.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.qgz\OpenWithProgids"; ValueType: none; ValueName: "BiruniGIS.Project"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.qgs\OpenWithProgids"; ValueType: none; ValueName: "BiruniGIS.Project"; Flags: uninsdeletevalue
; ArcGIS Pro projects, maps and layer files open in Biruni GIS from "Open with" (never as the default).
Root: HKA; Subkey: "Software\Classes\.aprx\OpenWithProgids"; ValueType: none; ValueName: "BiruniGIS.Project"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.mapx\OpenWithProgids"; ValueType: none; ValueName: "BiruniGIS.Project"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.lyrx\OpenWithProgids"; ValueType: none; ValueName: "BiruniGIS.Project"; Flags: uninsdeletevalue

[Run]
Filename: "{cmd}"; Parameters: "/c """"{app}\app\scripts\engine-postinstall.bat"" ""{app}\engine"""""; StatusMsg: "{cm:PreparingEngine}"; Flags: runhidden waituntilterminated
; Phone GPS: let phones on the network reach Biruni on the NMEA port only (UDP + TCP 10110).
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Biruni GIS GPS (UDP)"" dir=in action=allow protocol=UDP localport=10110 program=""{app}\engine\bin\qgis-bin.exe"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Biruni GIS GPS (TCP)"" dir=in action=allow protocol=TCP localport=10110 program=""{app}\engine\bin\qgis-bin.exe"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
; Phone GPS without an app: the phone's browser opens an HTTPS page on this PC (TCP 10111).
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Biruni GIS GPS (browser)"" dir=in action=allow protocol=TCP localport=10111 program=""{app}\engine\bin\qgis-bin.exe"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Biruni GIS GPS (UDP)"""; Flags: runhidden; RunOnceId: "DelFwUdp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Biruni GIS GPS (TCP)"""; Flags: runhidden; RunOnceId: "DelFwTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Biruni GIS GPS (browser)"""; Flags: runhidden; RunOnceId: "DelFwBrowser"

[UninstallDelete]
; files generated after installation (engine environment, Python caches)
Type: filesandordirs; Name: "{app}\engine"
Type: filesandordirs; Name: "{app}\app"
