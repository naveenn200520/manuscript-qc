Set objFSO = CreateObject("Scripting.FileSystemObject")
strScriptPath = objFSO.GetParentFolderName(WScript.ScriptFullName)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = strScriptPath
WshShell.Run Chr(34) & strScriptPath & "\Launch_Journal_Guard_App.bat" & Chr(34), 0, False
Set WshShell = Nothing
Set objFSO = Nothing
