Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

If Not fso.FolderExists(scriptDir & "\data") Then
    fso.CreateFolder(scriptDir & "\data")
End If

cmd = "python """ & scriptDir & "\work_tracker.py"" run"
shell.Run cmd, 0, False

WScript.Sleep 1200
statusCmd = "cmd /c python """ & scriptDir & "\work_tracker.py"" status > """ & scriptDir & "\data\status.log"" 2>&1"
statusCode = shell.Run(statusCmd, 0, True)

If statusCode = 0 Then
    shell.Run "cmd /c python """ & scriptDir & "\work_tracker.py"" notify started", 0, False
Else
    shell.Run "cmd /c python """ & scriptDir & "\work_tracker.py"" notify failed", 0, False
End If
