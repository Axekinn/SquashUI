@echo off
rem Builds dist\SquashUI\SquashUI.exe (folder build) and dist\SquashUI-portable.zip.
rem   build_exe.bat           folder build
rem   build_exe.bat onefile   a single SquashUI.exe (slower to start; unpacks to a temp folder each time)
setlocal
cd /d "%~dp0"

where python >nul 2>nul || (echo Python 3.10 or newer is required & exit /b 1)
if not exist .venv-build (python -m venv .venv-build || exit /b 1)
call .venv-build\Scripts\activate.bat || exit /b 1
python -m pip install --quiet --upgrade pip || exit /b 1
python -m pip install --quiet -r requirements-dev.txt || exit /b 1

echo.
echo === Tests ===
python -m unittest discover -s tests -t . || (echo Tests failed - not building & exit /b 1)

echo.
echo === Building ===
if /i "%1"=="onefile" set SQUASHUI_ONEFILE=1
pyinstaller --noconfirm --clean SquashUI.spec || exit /b 1

if /i "%1"=="onefile" (
    echo.
    echo Done: dist\SquashUI.exe
) else (
    powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\SquashUI\*' -DestinationPath 'dist\SquashUI-portable.zip'" || exit /b 1
    echo.
    echo Done: dist\SquashUI\SquashUI.exe  and  dist\SquashUI-portable.zip
)
endlocal
