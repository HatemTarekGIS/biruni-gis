# Releasing Biruni GIS

## 1. Prepare

1. Set `VERSION` in `app/plugins/biruni/version.py` (and `REPO` once the GitHub
   repository exists - it turns on update notices and problem-report links).
2. Run every test in `tests/` (see CONTRIBUTING.md) and the string check.
3. Build: `powershell -ExecutionPolicy Bypass -File scripts\build.ps1`
   → `dist\Biruni-GIS-<version>-Setup.exe` and its `.sha256`.

## 2. Code signing (removes the "Windows protected your PC" warning)

Free for open-source projects through the **SignPath Foundation**
(https://signpath.org). One-time steps:

1. Publish the source on GitHub with an OSI licence (GPL-2.0-or-later ✓).
2. Add a *Code signing policy* section to the README (who signs, from which
   build) - SignPath asks for it.
3. Apply at https://signpath.org/apply and connect the repository.
4. Signing then runs in CI on the built installer.

For a local certificate instead, set `BIRUNI_SIGN_CMD` before building, e.g.

```powershell
$env:BIRUNI_SIGN_CMD = 'signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a "{file}"'
```

## 3. Publish

```powershell
gh release create v<version> dist\Biruni-GIS-<version>-Setup.exe dist\Biruni-GIS-<version>-Setup.exe.sha256 `
   docs\guide\Biruni-GIS-Guide-ar.pdf `
   --title "Biruni GIS <version>" --notes-file docs\release-notes-<version>.md
```

Rebuild the Arabic guide first if the app changed: `engine\bin\python-qgis.bat docs\guide\build_guide.py`.

Biruni GIS checks `https://api.github.com/repos/<REPO>/releases/latest` once a
day and shows a *Download* notice when the tag (`v<version>`) is newer.

## 4. winget

1. Update `packaging/winget/<version>/`: `PackageVersion`, `InstallerUrl` (the
   release asset URL) and `InstallerSha256` (from the `.sha256` file).
2. Validate: `winget validate packaging\winget\<version>`
3. Submit with `wingetcreate submit packaging\winget\<version>` (or a pull
   request to https://github.com/microsoft/winget-pkgs).

## Installer size

The installer carries the full QGIS engine (~500 MB compressed). ECW, MrSID and
the Oracle client are removed (`scripts/engine-exclude.txt`) because their
licences do not allow redistribution.
