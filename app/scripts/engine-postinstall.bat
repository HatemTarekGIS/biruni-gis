@echo off
rem ---------------------------------------------------------------------------
rem Biruni GIS - engine post-install
rem
rem Runs the OSGeo4W post-install steps for the bundled QGIS engine WITHOUT
rem creating QGIS/OSGeo4W shortcuts or registering QGIS file associations
rem (Biruni GIS owns its own shortcuts and associations).
rem
rem Usage: engine-postinstall.bat "<engine root>"
rem ---------------------------------------------------------------------------
setlocal EnableExtensions

if "%~1"=="" (
  echo usage: %~nx0 "engine root"
  exit /b 2
)
if not exist "%~1\bin\qgis-bin.exe" (
  echo engine not found: %~1
  exit /b 3
)

set "OSGEO4W_ROOT=%~f1"
set OSGEO4W_MENU_LINKS=0
set OSGEO4W_DESKTOP_LINKS=0
set "OSGEO4W_STARTMENU=%TEMP%\biruni-no-links"
set "OSGEO4W_DESKTOP=%TEMP%\biruni-no-links"

call "%OSGEO4W_ROOT%\bin\o4w_env.bat"
pushd "%OSGEO4W_ROOT%"

rem Generic package scripts (textreplace templates, crssync, grass fontcap ...)
for %%f in ("etc\postinstall\*.bat") do (
  if /i not "%%~nxf"=="qgis.bat" (
    echo [biruni] %%~nxf
    call "%%~ff" >nul 2>&1
    ren "%%~ff" "%%~nxf.done" >nul 2>&1
  )
)

rem QGIS itself: only generate bin\qgis-bin.env (no links, no .qgz association)
call "%OSGEO4W_ROOT%\bin\qt6_env.bat"
set "QGIS_WIN_APP_NAME=Biruni GIS"
path %OSGEO4W_ROOT%\apps\qgis\bin;%PATH%
set "QGIS_PREFIX_PATH=%OSGEO4W_ROOT:\=/%/apps/qgis"
set GDAL_FILENAME_IS_UTF8=YES
set VSI_CACHE=TRUE
set VSI_CACHE_SIZE=1000000
set "QT_PLUGIN_PATH=%OSGEO4W_ROOT%\apps\qgis\qtplugins;%OSGEO4W_ROOT%\apps\qt6\plugins"
"%OSGEO4W_ROOT%\bin\qgis-bin.exe" --postinstall
if exist "etc\postinstall\qgis.bat" ren "etc\postinstall\qgis.bat" "qgis.bat.done" >nul 2>&1
del /s /q "%OSGEO4W_ROOT%\apps\qgis\python\*.pyc" >nul 2>&1

popd
if not exist "%OSGEO4W_ROOT%\bin\qgis-bin.env" (
  echo [biruni] qgis-bin.env was not generated
  exit /b 4
)
echo [biruni] engine ready
exit /b 0
