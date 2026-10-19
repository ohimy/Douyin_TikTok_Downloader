@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Build the Windows desktop executable. Missing prerequisites are installed
rem through winget so a clean Windows workstation can build this project.

call :find_python
if not defined PYTHON_EXE (
  echo Python 3.11+ was not found. Installing Python 3.12 with winget...
  call :require_winget || goto :failed
  call :install_package Python.Python.3.12 "Python 3.12" || goto :failed
  call :find_python
)
if not defined PYTHON_EXE (
  echo Python installation completed, but this terminal cannot locate it.
  echo Close this terminal, open a new Command Prompt, and run this script again.
  goto :failed
)

call :find_node
call :node_supported
if errorlevel 1 (
  echo Node.js 20+ was not found. Installing the current Node.js LTS with winget...
  call :require_winget || goto :failed
  call :install_package OpenJS.NodeJS.LTS "Node.js LTS" || goto :failed
  call :find_node
  call :node_supported
)
if errorlevel 1 (
  echo Node.js 20+ is required for the Xiaohongshu signing runtime.
  goto :failed
)
for %%D in ("!NODE_EXE!") do set "PATH=%%~dpD;!PATH!"

call :find_chrome
call :find_edge
if not defined CHROME_EXE if not defined EDGE_EXE (
  echo Google Chrome was not found. Installing it for isolated account login...
  call :require_winget || goto :failed
  call :install_package Google.Chrome "Google Chrome" || goto :failed
  call :find_chrome
)
if not defined CHROME_EXE if not defined EDGE_EXE (
  echo Install Chrome or Microsoft Edge for the desktop app's QR-code and password login window.
  goto :failed
)

echo.
echo Preparing Python environment with "!PYTHON_EXE!"...
"!PYTHON_EXE!" -m venv .venv || goto :failed
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
"!VENV_PYTHON!" -m pip install --upgrade pip || goto :failed
"!VENV_PYTHON!" -m pip install -r requirements-local.txt || goto :failed
"!VENV_PYTHON!" tools\fetch_xhs_assets.py || goto :failed

"!VENV_PYTHON!" -m PyInstaller --noconfirm --clean --onefile --noconsole --name DouyinLocalAPI --add-data "%CD%\src\dtk\signing\native;dtk\signing\native" --add-data "%CD%\local_api\xhs_assets;local_api\xhs_assets" --add-binary "!NODE_EXE!;local_api\xhs_assets\node" --collect-all playwright --collect-all execjs --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto desktop_main.py || goto :failed

echo.
echo Build succeeded: dist\DouyinLocalAPI.exe
echo The executable opens the native Windows management window. Chrome is preferred and Edge is supported for account login.
goto :done

:find_python
set "PYTHON_EXE="
for /f "delims=" %%P in ('py -3 -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor in range(11, 100); print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE for /f "delims=" %%P in ('python -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor in range(11, 100); print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
for %%P in ("%LocalAppData%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles(x86)%\Python312\python.exe") do (
  if not defined PYTHON_EXE if exist "%%~fP" set "PYTHON_EXE=%%~fP"
)
exit /b 0

:find_node
set "NODE_EXE="
for /f "delims=" %%N in ('where node 2^>nul') do if not defined NODE_EXE set "NODE_EXE=%%N"
for %%N in ("%ProgramFiles%\nodejs\node.exe" "%LocalAppData%\Programs\nodejs\node.exe") do (
  if not defined NODE_EXE if exist "%%~fN" set "NODE_EXE=%%~fN"
)
exit /b 0

:node_supported
set "NODE_MAJOR="
if not defined NODE_EXE exit /b 1
for /f "tokens=1 delims=." %%V in ('"!NODE_EXE!" --version 2^>nul') do set "NODE_MAJOR=%%V"
set "NODE_MAJOR=!NODE_MAJOR:v=!"
if not defined NODE_MAJOR exit /b 1
if !NODE_MAJOR! GEQ 20 exit /b 0
exit /b 1

:find_chrome
set "CHROME_EXE="
for %%C in ("%ProgramFiles%\Google\Chrome\Application\chrome.exe" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" "%LocalAppData%\Google\Chrome\Application\chrome.exe") do (
  if not defined CHROME_EXE if exist "%%~fC" set "CHROME_EXE=%%~fC"
)
exit /b 0

:find_edge
set "EDGE_EXE="
for %%E in ("%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" "%LocalAppData%\Microsoft\Edge\Application\msedge.exe") do (
  if not defined EDGE_EXE if exist "%%~fE" set "EDGE_EXE=%%~fE"
)
exit /b 0

:require_winget
where winget >nul 2>&1
if errorlevel 1 (
  echo winget is unavailable. Install Microsoft App Installer from the Microsoft Store,
  echo then run this script again. Alternatively install the missing prerequisite manually.
  exit /b 1
)
exit /b 0

:install_package
echo Installing %~2...
winget install --id %~1 --exact --silent --accept-package-agreements --accept-source-agreements
exit /b %errorlevel%

:failed
echo.
echo Build did not complete. See the messages above, fix the prerequisite, then run this script again.
endlocal
exit /b 1

:done
endlocal
exit /b 0
