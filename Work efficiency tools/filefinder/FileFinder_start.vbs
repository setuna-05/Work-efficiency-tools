' FileFinder launcher: starts filefinder.py with NO black console window.
' Put this file in the same folder as filefinder.py and double-click it.
Option Explicit
Dim fso, sh, dir, target, cmds, styles, i, ok, f, n, listing

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

' Find a .py / .pyw file whose name starts with "filefinder"
' (also matches names like "filefinder (1).py")
target = ""
listing = ""
For Each f In fso.GetFolder(dir).Files
    n = LCase(f.Name)
    listing = listing & vbCrLf & f.Name
    If Left(n, 10) = "filefinder" Then
        If Right(n, 3) = ".py" Or Right(n, 4) = ".pyw" Then
            target = f.Path
        End If
    End If
Next

If target = "" Then
    MsgBox "No filefinder .py file was found in this folder:" & vbCrLf & dir & _
           vbCrLf & vbCrLf & "Files in this folder:" & listing, 48, "FileFinder"
    WScript.Quit 1
End If

sh.CurrentDirectory = dir

' pyw / pythonw have no console window, so they must be started with a NORMAL
' window style (1). If they are started hidden (0), the app's own window is
' hidden too and never appears.
' py / python open a console window, so those are started hidden (0).
cmds = Array("pyw", "pythonw", "py", "python")
styles = Array(1, 1, 0, 0)
ok = False

On Error Resume Next
For i = 0 To UBound(cmds)
    Err.Clear
    sh.Run cmds(i) & " """ & target & """", styles(i), False
    If Err.Number = 0 Then
        ok = True
        Exit For
    End If
Next
On Error GoTo 0

If Not ok Then
    MsgBox "Python could not be started. Please check that Python is installed.", 48, "FileFinder"
End If
