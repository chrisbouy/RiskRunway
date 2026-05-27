@echo off
REM Build RiskRunwayLauncher for Windows
REM Creates launcher with hidden console (VBScript wrapper) and registry entries

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

REM ─────────────────────────────────────────────────────────────────────────────
REM Create VBScript launcher (hides console window from the start)
REM This is the protocol handler entry point — no cmd window ever appears.
REM ─────────────────────────────────────────────────────────────────────────────
echo Creating VBScript launcher (hidden console)...
(
echo ' RiskRunwayLauncher.vbs — Protocol handler for riskrunway:// URLs
echo ' Launches local_agent.py with pythonw.exe (no console window)
echo ' Falls back to python.exe with hidden window via WScript.Shell
echo.
echo Dim url, jobId, server, agentPath, pythonExe, cmd
echo Dim fso, shell
echo Set fso = CreateObject("Scripting.FileSystemObject"^)
echo Set shell = CreateObject("WScript.Shell"^)
echo.
echo ' Get the URL from command line
echo If WScript.Arguments.Count = 0 Then
echo     WScript.Quit 1
echo End If
echo url = WScript.Arguments(0^)
echo.
echo ' Parse job_id from URL
echo Dim parts, queryStr, params, i, pair
echo If InStr(url, "?"^) ^> 0 Then
echo     queryStr = Mid(url, InStr(url, "?"^) + 1^)
echo Else
echo     WScript.Quit 1
echo End If
echo.
echo ' Split query string into parameters
echo params = Split(queryStr, "^&"^)
echo jobId = ""
echo server = ""
echo For i = 0 To UBound(params^)
echo     pair = Split(params(i^), "=", 2^)
echo     If UBound(pair^) ^>= 1 Then
echo         If LCase(pair(0^)^) = "job_id" Then
echo             jobId = pair(1^)
echo         ElseIf LCase(pair(0^)^) = "server" Then
echo             server = Unescape(pair(1^)^)
echo         End If
echo     End If
echo Next
echo.
echo If jobId = "" Or server = "" Then
echo     MsgBox "Invalid RiskRunway URL: missing job_id or server", vbCritical, "RiskRunway"
echo     WScript.Quit 1
echo End If
echo.
echo ' Find local_agent.py
echo Dim scriptDir
echo scriptDir = fso.GetParentFolderName(WScript.ScriptFullName^)
echo agentPath = scriptDir ^& "\local_agent.py"
echo If Not fso.FileExists(agentPath^) Then
echo     MsgBox "Could not find local_agent.py in " ^& scriptDir, vbCritical, "RiskRunway"
echo     WScript.Quit 1
echo End If
echo.
echo ' Find pythonw.exe (windowless Python) — preferred
echo ' Fall back to python.exe if pythonw not found
echo pythonExe = ""
echo.
echo ' Check if pythonw.exe is on PATH
echo On Error Resume Next
echo Dim execResult
echo execResult = shell.Run("cmd /c where pythonw.exe ^> nul 2^>^&1", 0, True^)
echo If execResult = 0 Then
echo     pythonExe = "pythonw.exe"
echo End If
echo On Error GoTo 0
echo.
echo ' Fall back to python.exe
echo If pythonExe = "" Then
echo     pythonExe = "python.exe"
echo End If
echo.
echo ' Build command and run hidden (vbHide = 0)
echo cmd = """" ^& pythonExe ^& """ """ ^& agentPath ^& """ --job-id " ^& jobId ^& " --server " ^& server
echo.
echo ' Run with hidden window (0 = vbHide)
echo shell.Run cmd, 0, False
echo.
echo WScript.Quit 0
) > "%DIST_DIR%\RiskRunwayLauncher.vbs"

echo [OK] Created RiskRunwayLauncher.vbs

REM ─────────────────────────────────────────────────────────────────────────────
REM Also create the .bat as a fallback / manual testing tool
REM ─────────────────────────────────────────────────────────────────────────────
echo Creating fallback .bat launcher...
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
echo REM Use the VBScript launcher (hidden console)
echo wscript.exe "%%~dp0RiskRunwayLauncher.vbs" "%%URL%%"
echo exit /b 0
) > "%DIST_DIR%\RiskRunwayLauncher.bat"

echo [OK] Created RiskRunwayLauncher.bat (delegates to .vbs)

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
echo 1. Copy this folder to a permanent location (e.g., C:\Users\YourName\AppData\Local\RiskRunway\)
echo 2. Run install.bat
echo.
echo The install.bat script will register the riskrunway:// protocol handler
echo in the Windows Registry.
echo.
echo After installation, clicking "Export to AMS" in the RiskRunway web app
echo will automatically launch the agent with NO visible console window.
echo.
echo Uninstallation:
echo Run uninstall.bat to remove registry entries.
) > "%DIST_DIR%\README.txt"

REM ─────────────────────────────────────────────────────────────────────────────
REM Create install.bat — registers .vbs as the protocol handler
REM ─────────────────────────────────────────────────────────────────────────────
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
echo REM Register protocol handler in registry (uses wscript.exe to run .vbs silently)
echo reg add "HKCU\Software\Classes\riskrunway" /f
echo reg add "HKCU\Software\Classes\riskrunway" /ve /t REG_SZ /d "URL:RiskRunway Protocol" /f
echo reg add "HKCU\Software\Classes\riskrunway" /v "URL Protocol" /t REG_SZ /d "" /f
echo reg add "HKCU\Software\Classes\riskrunway\shell\open\command" /f
echo reg add "HKCU\Software\Classes\riskrunway\shell\open\command" /ve /t REG_SZ /d "wscript.exe \"%%INSTALL_DIR%%\RiskRunwayLauncher.vbs\" \"%%1\"" /f
echo.
echo echo.
echo echo [OK] Protocol handler registered
echo echo.
echo echo Installation complete!
echo echo.
echo echo You can now use RiskRunway Export from your browser.
echo echo No console window will appear during export.
echo echo.
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
echo 1. Copy the folder to your preferred location
echo 2. Run install.bat
echo 3. Re-run install.bat if you move the folder
echo.
echo The protocol handler now uses wscript.exe + VBScript
echo to launch Python with NO visible console window.
echo ============================================
pause
