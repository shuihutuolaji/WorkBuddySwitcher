@echo off
rem ???? WorkBuddy ????????? C:\Users\W\WorkBuddySwitcher
cd /d "%~dp0"
"C:\Users\W\AppData\Local\Programs\Python\Python312\python.exe" -m PyInstaller --noconfirm --clean --onedir --noconsole ^
  --hidden-import psutil --hidden-import cryptography --hidden-import websockets ^
  --name WorkBuddySwitcher --icon wbicon.ico workbuddy_switcher.py
if errorlevel 1 goto :err
xcopy /e /i /y "dist\WorkBuddySwitcher" "E:\99 tool\WorkBuddySwitcher" >nul
echo.
echo ??: E:\99 tool\WorkBuddySwitcher\WorkBuddySwitcher.exe????????????
goto :eof
:err
echo ????
pause
