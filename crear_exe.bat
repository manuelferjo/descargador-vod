@echo off
chcp 65001 >nul
title Crear DescargadorYouTube.exe
cd /d "%~dp0"

echo ==============================================
echo   Creando DescargadorYouTube.exe
echo ==============================================
echo.

if not exist "descargador_youtube.py" (
    echo [ERROR] Pon este archivo en la misma carpeta que descargador_youtube.py
    pause & exit /b 1
)

echo [1/4] Instalando herramientas (yt-dlp, Pillow y PyInstaller)...
python -m pip install -U yt-dlp pillow pyinstaller || (echo [ERROR] Fallo pip & pause & exit /b 1)

echo.
echo [2/4] Generando el ejecutable (puede tardar un par de minutos)...
rem --onedir en lugar de --onefile: los .exe autoextraibles son los que mas
rem falsos positivos dan. --noupx: la compresion UPX tambien los dispara.
set "VER="
if exist "version_info.txt" set "VER=--version-file version_info.txt"
rem res\ lleva el logo, el icono y las fuentes del estilo Vortexs.
python -m PyInstaller --noconfirm --clean --onedir --windowed --noupx %VER% ^
    --icon res\app.ico --add-data "res;res" --exclude-module numpy ^
    --name DescargadorYouTube --collect-all yt_dlp descargador_youtube.py
if errorlevel 1 (echo [ERROR] Fallo PyInstaller & pause & exit /b 1)

echo.
echo [3/4] Preparando carpeta para compartir...
if exist "Compartir" rmdir /s /q "Compartir"
xcopy /e /i /q /y "dist\DescargadorYouTube" "Compartir" >nul
if exist "Guia.html" (copy /y "Guia.html" "Compartir\LEEME - Guia de uso.html" >nul) else (echo     [AVISO] No esta Guia.html en la carpeta; el zip ira sin guia.)

echo.
echo [4/4] Buscando FFmpeg para incluirlo...
set "FF="
for /f "delims=" %%i in ('where ffmpeg 2^>nul') do if not defined FF set "FF=%%i"
if defined FF (
    rem Solo ffmpeg.exe: ffprobe.exe no hace falta y duplicaria el tamano del zip.
    copy /y "%FF%" "Compartir\" >nul
    echo     FFmpeg incluido.
) else (
    echo     [AVISO] No se encontro FFmpeg. Instalalo con:  winget install ffmpeg
    echo     y vuelve a ejecutar este script, o copia ffmpeg.exe a la carpeta Compartir.
)

rem Nombre claro para no confundirlo con la carpeta del codigo al compartirlo.
set "ZIP=DescargadorVOD-v1.3-PARA-COMPARTIR.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'Compartir\*' -DestinationPath '%ZIP%' -Force"

echo.
echo ==============================================
echo   LISTO
echo   Comparte SOLO este archivo: %ZIP%
echo ==============================================
explorer "%~dp0"
pause
