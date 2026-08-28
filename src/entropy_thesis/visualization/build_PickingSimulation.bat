@echo off
setlocal EnableExtensions

rem ============================================================================
rem PickingSimulation.exe build script
rem Place this BAT beside PickingSimulation.spec and picking_animation_desktop.py
rem under src\entropy_thesis\visualization.
rem ============================================================================

set "VIS_DIR=%~dp0"
for %%I in ("%VIS_DIR%\..\..\..") do set "PROJECT_ROOT=%%~fI"

pushd "%PROJECT_ROOT%" || goto :fail

echo.
echo [ROOT ] %PROJECT_ROOT%
echo [PY   ] Checking Python...
python --version || goto :fail

echo.
echo [DEPS ] Installing/updating build dependencies...
python -m pip install --upgrade "PySide6>=6.7" "pyinstaller>=6.10" orjson || goto :fail

echo.
echo [BUILD] Building PickingSimulation.exe...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --distpath "%PROJECT_ROOT%\dist" ^
  --workpath "%PROJECT_ROOT%\build\PickingSimulation" ^
  "%VIS_DIR%PickingSimulation.spec" || goto :fail

if not exist "%PROJECT_ROOT%\dist\PickingSimulation.exe" goto :fail

echo.
echo ================================================================
echo BUILD SUCCESS

echo EXE: %PROJECT_ROOT%\dist\PickingSimulation.exe
echo ================================================================
echo.
echo The EXE automatically searches the project root one level above dist\ for:
echo   results\figures\picking_animation_actual_data

echo   results\figures\picking_animation_actual.html   ^(optional^)
echo   data\raw_original\Layout_Z1.0.svg

echo   data\raw\Support_Points_Navigation.csv

echo.
echo Double-click dist\PickingSimulation.exe to run it.
echo.
popd
pause
exit /b 0

:fail
echo.
echo ================================================================
echo BUILD FAILED - see the messages above.
echo ================================================================
if defined PROJECT_ROOT popd
pause
exit /b 1
