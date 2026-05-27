' RiskRunwayLauncher.vbs — Protocol handler for riskrunway:// URLs
' Launches local_agent.py with pythonw.exe (no console window)
' Falls back to python.exe with hidden window via WScript.Shell

Dim url, jobId, server, agentPath, pythonExe, cmd
Dim fso, shell
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Get the URL from command line
If WScript.Arguments.Count = 0 Then
    WScript.Quit 1
End If
url = WScript.Arguments(0)

' Parse job_id and server from URL query string
Dim queryStr, params, i, pair
If InStr(url, "?") > 0 Then
    queryStr = Mid(url, InStr(url, "?") + 1)
Else
    WScript.Quit 1
End If

' Split query string into parameters
params = Split(queryStr, "&")
jobId = ""
server = ""
For i = 0 To UBound(params)
    pair = Split(params(i), "=", 2)
    If UBound(pair) >= 1 Then
        If LCase(pair(0)) = "job_id" Then
            jobId = pair(1)
        ElseIf LCase(pair(0)) = "server" Then
            server = Unescape(pair(1))
        End If
    End If
Next

If jobId = "" Or server = "" Then
    MsgBox "Invalid RiskRunway URL: missing job_id or server", vbCritical, "RiskRunway"
    WScript.Quit 1
End If

' Find local_agent.py in same directory as this script
Dim scriptDir
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
agentPath = scriptDir & "\local_agent.py"
If Not fso.FileExists(agentPath) Then
    MsgBox "Could not find local_agent.py in " & scriptDir, vbCritical, "RiskRunway"
    WScript.Quit 1
End If

' Find pythonw.exe (windowless Python) — preferred
' Fall back to python.exe if pythonw not found
pythonExe = ""

' Check if pythonw.exe is on PATH
On Error Resume Next
Dim execResult
execResult = shell.Run("cmd /c where pythonw.exe > nul 2>&1", 0, True)
If execResult = 0 Then
    pythonExe = "pythonw.exe"
End If
On Error GoTo 0

' Fall back to python.exe
If pythonExe = "" Then
    pythonExe = "python.exe"
End If

' Build command and run hidden (0 = vbHide)
cmd = """" & pythonExe & """ """ & agentPath & """ --job-id " & jobId & " --server " & server

' Run with hidden window — no console appears at all
shell.Run cmd, 0, False

WScript.Quit 0
