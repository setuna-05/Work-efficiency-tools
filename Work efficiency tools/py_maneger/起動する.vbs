Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(folder, "app_manager.py")
CreateObject("WScript.Shell").Run "pyw.exe """ & script & """", 0, False
