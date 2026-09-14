Set WshShell = CreateObject("WScript.Shell")
base = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run """" & base & "\iniciar_servidor.bat" & """", 0, False
WScript.Sleep 2500
WshShell.Run "http://127.0.0.1:5000", 1, False
