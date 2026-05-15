Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batchPath = fso.BuildPath(scriptDir, "frissites.bat")

If Not fso.FileExists(batchPath) Then
    MsgBox "Nem talalom a frissites.bat fajlt:" & vbCrLf & batchPath, vbCritical, "UMKGL Bot Frissito"
    WScript.Quit 1
End If

shell.Run "cmd.exe /c """ & batchPath & """", 1, False
