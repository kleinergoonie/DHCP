@echo off
:: ============================================================
:: build.bat – Baut DHCPServer.exe und den Windows-Installer
:: Benötigt: Python 3.11+, PyInstaller, Inno Setup 6
:: ============================================================

setlocal

echo.
echo ==========================================
echo  DHCP Server – Build-Skript
echo ==========================================
echo.

:: ── 1. Abhängigkeiten installieren ──────────────────────────
echo [1/3] Installiere Python-Abhaengigkeiten ...
pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo FEHLER: pip install fehlgeschlagen.
    pause & exit /b 1
)
echo       OK

:: ── 2. PyInstaller – einzelne .exe ──────────────────────────
echo [2/3] Erstelle DHCPServer.exe mit PyInstaller ...
pyinstaller --clean --noconfirm build.spec
if errorlevel 1 (
    echo FEHLER: PyInstaller fehlgeschlagen.
    pause & exit /b 1
)
echo       OK – dist\DHCPServer.exe erstellt

:: ── 3. Inno Setup – Installer ───────────────────────────────
echo [3/3] Erstelle Windows-Installer mit Inno Setup ...

:: Typische Installationspfade für Inno Setup 6
set ISCC=""
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"

if %ISCC%=="" (
    echo WARNUNG: Inno Setup nicht gefunden.
    echo          Installiere Inno Setup 6 von https://jrsoftware.org/isdl.php
    echo          und fuhre anschliessend 'ISCC.exe installer.iss' manuell aus.
    pause & exit /b 0
)

%ISCC% installer.iss
if errorlevel 1 (
    echo FEHLER: Inno Setup fehlgeschlagen.
    pause & exit /b 1
)

echo.
echo ==========================================
echo  Fertig!
echo  EXE:       dist\DHCPServer.exe
echo  Installer: installer_output\DHCPServer_Setup_1.0.0.exe
echo ==========================================
echo.
pause
