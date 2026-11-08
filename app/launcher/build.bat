@echo off
rem Builds ..\..\LAN Hub.exe from LANHub.cs with the C# compiler that ships with Windows (.NET Framework 4).
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
"%CSC%" /nologo /target:winexe /optimize+ /platform:anycpu /win32icon:"%~dp0..\assets\app.ico" /r:System.Windows.Forms.dll /r:System.Drawing.dll /out:"%~dp0..\..\LAN Hub.exe" "%~dp0LANHub.cs"
