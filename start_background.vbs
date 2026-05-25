Option Explicit

Dim shell, fso, scriptDir, dataDir
Dim cmd, statusCmd, statusCode, launchCode, attempt

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
dataDir = scriptDir & "\data"
shell.CurrentDirectory = scriptDir

If Not fso.FolderExists(dataDir) Then
    fso.CreateFolder(dataDir)
End If

AppendStartupLog "requesting background startup"
cmd = "cmd /c python """ & scriptDir & "\work_tracker.py"" run 1>>""" & dataDir & "\bg_out.log"" 2>>""" & dataDir & "\bg_err.log"""
launchCode = shell.Run(cmd, 0, False)
AppendStartupLog "launcher returned code " & launchCode

statusCode = 1
For attempt = 1 To 20
    WScript.Sleep 250
    statusCmd = "cmd /c python """ & scriptDir & "\work_tracker.py"" status > """ & dataDir & "\status.log"" 2>&1"
    statusCode = shell.Run(statusCmd, 0, True)
    If statusCode = 0 Then
        Exit For
    End If
Next

If statusCode = 0 Then
    AppendStartupLog "startup confirmed after attempt " & attempt
    shell.Run "cmd /c python """ & scriptDir & "\work_tracker.py"" notify started", 0, False
Else
    AppendStartupLog "startup confirmation failed; see status.log and bg_err.log"
    shell.Run "cmd /c python """ & scriptDir & "\work_tracker.py"" notify failed", 0, False
End If

Sub AppendStartupLog(message)
    Dim logFile, stamp
    stamp = Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & " " _
        & Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) & ":" & Right("0" & Second(Now), 2)
    Set logFile = fso.OpenTextFile(dataDir & "\startup.log", 8, True)
    logFile.WriteLine "[" & stamp & "] " & message
    logFile.Close
End Sub
