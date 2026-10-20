@echo off
rem AudioLab launcher for Windows.
rem   start.bat              install on first run (or after an update), then open the app
rem   start.bat --with-ai    also install the optional AI software; models download on first use
rem   start.bat --reinstall  start over with a fresh environment
rem App packages live in .venv; Python, app data and AI caches may live elsewhere.
rem See docs/INSTALLATION.md before removing a folder that may contain your audio.
setlocal
rem pushd (not cd) also works when the folder is on a network share (\\server\...).
pushd "%~dp0" || (echo Could not open the AudioLab folder. & pause & exit /b 1)
set "PY=.venv\Scripts\python.exe"
set "STAMP=.venv\.audiolab-installed"
set "PYTHONUTF8=1"

if /I "%~1"=="--reinstall" if exist ".venv" rmdir /s /q ".venv"

if not exist "%PY%" (
  echo Setting up AudioLab ^(first run; downloads may take several minutes^)...
  call :makevenv || goto :nopython
)

rem (Re)install when pyproject.toml changed since the last *successful* install.
"%PY%" -c "import hashlib,pathlib,sys; h=hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest(); s=pathlib.Path(r'%STAMP%'); sys.exit(0 if s.is_file() and s.read_text().strip()==h else 1)"
if errorlevel 1 (
  echo Installing AudioLab and its parts ^(ffmpeg, downloader^)...
  "%PY%" -m pip install --disable-pip-version-check -e . || goto :failed
  "%PY%" -c "import hashlib,pathlib; pathlib.Path(r'%STAMP%').write_text(hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest())"
)

if /I "%~1"=="--with-ai" (
  echo Installing AI software ^(model files may also download on first use^)...
  "%PY%" -m pip install --disable-pip-version-check -e .[ai] || goto :failed
)

"%PY%" -m audiolab app
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" pause
popd
exit /b %APP_EXIT%

:makevenv
rem Prefer uv, then the py launcher, then a python.exe on PATH (3.10 - 3.13).
where uv >nul 2>nul && (uv venv --seed --python 3.12 .venv && exit /b 0)
for %%V in (3.12 3.11 3.13 3.10) do (
  py -%%V -m venv .venv >nul 2>nul && exit /b 0
)
python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>nul && (python -m venv .venv && exit /b 0)
exit /b 1

:nopython
echo.
echo AudioLab needs Python 3.10 - 3.13.
echo Install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo or install uv:  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
echo Then run start.bat again.
pause
popd
exit /b 1

:failed
echo.
echo Installation failed - see the messages above. Run "start.bat" again, or ask for help with the output.
pause
popd
exit /b 1
