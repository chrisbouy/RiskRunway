@echo off
REM Build RiskRunwayLauncher for Windows
REM Creates launcher executable and registry entries for protocol handler

setlocal EnableDelayedExpansion

set SCRIPT_DIR=%~dp0
set PROJECT_ROOT=%SCRIPT_DIR%..
set BUILD_DIR=%SCRIPT_DIR%build
set DIST_DIR=%BUILD_DIR%\RiskRunwayLauncher

echo Building RiskRunwayLauncher for Windows...
echo.

REM Clean previous build
if exist "%DIST_DIR%" rmdir /S /Q "%DIST_DIR%"
mkdir "%DIST_DIR%"

REM Create launcher script
echo Creating launcher script...
(
echo @echo off
echo setlocal EnableDelayedExpansion
echo.
echo REM Get the URL from command line argument
echo set "URL=%%~1"
echo.
echo if "%%URL%%"=="" (
echo     echo Usage: RiskRunwayLauncher ^<riskrunway://...^>
echo     pause
echo     exit /b 1
echo ^)
echo.
echo REM Parse the URL to extract job_id and server
echo for /f "tokens=2 delims==" %%%%a in ^("%%URL%%"^) do set "JOB_ID_ARG=%%%%a"
echo for /f "tokens=1 delims=^&" %%%%a in ^("%%JOB_ID_ARG%%"^) do set "JOB_ID=%%%%a"
echo.
echo for /f "tokens=3 delims==" %%%%a in ^("%%URL%%"^) do set "SERVER=%%%%a"
echo.
echo echo Starting RiskRunway Export for Job #%%JOB_ID%%
echo echo Server: %%SERVER%%
echo.
echo.
echo REM Find Python
echo set "PYTHON=python"
echo where python3 ^>nul 2^>^&1 ^&^& set "PYTHON=python3"
echo.
echo REM Find local_agent.py
echo set "AGENT_PATH=%SCRIPT_DIR%local_agent.py"
echo if not exist "%%AGENT_PATH%%" (
echo     set "AGENT_PATH=%PROJECT_ROOT%\local_agent.py"
echo ^)
echo.
echo if not exist "%%AGENT_PATH%%" (
echo     echo Error: Could not find local_agent.py
echo     echo Please ensure RiskRunway is installed correctly.
echo     pause
echo     exit /b 1
echo ^)
echo.
echo REM Launch local_agent in a new Command Prompt window
echo start "RiskRunway Export" cmd /k "%%PYTHON%% "%%AGENT_PATH%%" --job-id %%JOB_ID%% --server %%SERVER%% ^&^& pause"
echo.
echo exit /b 0
) > "%DIST_DIR%\RiskRunwayLauncher.bat"

REM Copy local_agent.py if it exists
if exist "%PROJECT_ROOT%\local_agent.py" (
    copy "%PROJECT_ROOT%\local_agent.py" "%DIST_DIR%\"
    echo [OK] Copied local_agent.py
)

REM Create README
echo Creating README...
(
echo RiskRunway Launcher for Windows
echo =================================
echo.
echo Installation:
echo 1. Copy this folder to C:\Program Files\RiskRunway\ or your preferred location
echo 2. Run install.bat as Administrator
echo.
echo The install.bat script will register the riskrunway:// protocol handler
echo in the Windows Registry.
echo.
echo After installation, clicking "Export to AMS" in the RiskRunway web app
echo will automatically launch this application.
echo.
echo Uninstallation:
echo Run uninstall.bat as Administrator to remove registry entries.
) > "%DIST_DIR%\README.txt"

REM Create install.bat
echo Creating install script...
(
echo @echo off
echo echo Installing RiskRunway Launcher...
echo echo.
echo.
echo REM Get current directory
echo for %%%%F in ^("%%~dp0."^) do set "INSTALL_DIR=%%%%~fF"
echo.
echo echo Installing from: %%INSTALL_DIR%%
echo.
echo REM Register protocol handler in registry
echo reg add "HKCU\Software\Classes\riskrunway" /f
echo reg add "HKCU\Software\Classes\riskrunway" /ve /t REG_SZ /d "URL:RiskRunway Protocol" /f
echo reg add "HKCU\Software\Classes\riskrunway" /v "URL Protocol" /t REG_SZ /d "" /f
echo reg add "HKCU\Software\Classes\riskrunway\shell\open\command" /f
echo reg add "HKCU\Software\Classes\riskrunway\shell\open\command" /ve /t REG_SZ /d "\"%%INSTALL_DIR%%\RiskRunwayLauncher.bat\" \"%%1\"" /f
echo.
echo echo [OK] Protocol handler registered
echo.
echo Installation complete!
echo.
echo You can now use RiskRunway Export from your browser.
echo.
echo Test it:
echo   start riskrunway://export?job_id=123^^^&server=https://example.com
echo.
echo pause
) > "%DIST_DIR%\install.bat"

REM Create uninstall.bat
echo Creating uninstall script...
(
echo @echo off
echo echo Uninstalling RiskRunway Launcher...
echo echo.
echo reg delete "HKCU\Software\Classes\riskrunway" /f
echo if errorlevel 1 (
echo     echo [ERROR] Could not remove registry entries
echo     echo Make sure you run as Administrator
echo ^) else (
echo     echo [OK] Protocol handler removed
echo ^)
echo.
echo echo Uninstall complete.
echo echo You can now delete this folder.
echo pause
) > "%DIST_DIR%\uninstall.bat"

echo.
echo ============================================
echo Build complete!
echo.
echo Location: %DIST_DIR%
echo.
echo Next steps:
echo 1. Copy the folder to your preferred location (e.g., C:\Program Files\RiskRunway\)
echo 2. Run install.bat as Administrator
echo.
echo ============================================
pause
