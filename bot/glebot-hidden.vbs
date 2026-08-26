' Обёртка для планировщика: запускает glebot.cmd без консольного окна.
' Без неё при каждом входе в Windows на экране появлялось бы чёрное окно,
' которое хочется закрыть — а закрытие убивает бота.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.Run """" & here & "\glebot.cmd""", 0, False
