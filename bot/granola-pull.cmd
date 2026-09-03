@echo off
REM Выгрузка встречи из Granola в Downloads\granola_inbox\
REM   granola-pull.cmd --list      показать последние встречи
REM   granola-pull.cmd             выгрузить самую свежую
REM   granola-pull.cmd --index 1   выгрузить предыдущую
"C:\Program Files\Python314\python.exe" "%~dp0granola_pull.py" %*
