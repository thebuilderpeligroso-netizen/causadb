# CausaDB Windows SFX builder
$ErrorActionPreference = "Stop"

Write-Host "Building CausaDB Windows SFX installer..."

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputDir = if ($args[0]) { $args[0] } else { "$scriptDir\dist" }
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

# Create the SFX stub (a .bat that extracts and runs the ps1)
$sfxStub = @"
@echo off
setlocal
set TEMPDIR=%TEMP%\causadb_install_%RANDOM%
mkdir "%TEMPDIR%"
cd /d "%TEMPDIR%"
echo Extracting CausaDB installer...
powershell -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%~f0' -DestinationPath '.' -Force"
powershell -ExecutionPolicy Bypass -File .\installer_template.ps1
set EXITCODE=%ERRORLEVEL%
cd /d "%TEMPDIR%\.."
rmdir /s /q "%TEMPDIR%"
exit /b %EXITCODE%
"@

# Create the archive with installer ps1 embedded
$archivePath = "$outputDir\causadb-windows-installer.zip"
Compress-Archive -Path "$scriptDir\installer_template.ps1" -DestinationPath $archivePath -Force

# Combine stub + archive into a self-extracting .bat
$sfxPath = "$outputDir\causadb-installer.bat"
$sfxStub | Out-File -FilePath $sfxPath -Encoding ascii
Add-Content -Path $sfxPath -Value ""
Get-Content -Path $archivePath -Encoding Byte -ReadCount 0 | Add-Content -Path $sfxPath -Encoding Byte

Remove-Item -Path $archivePath -Force

Write-Host "SFX installer built: $sfxPath" -ForegroundColor Green
