@echo off
title UMKGL Bot Frissito
color 0A

echo ========================================================
echo   UMKGL BOT - AUTOMATA FRISSITES ES UJRAINDITAS
echo ========================================================
echo.

echo [1/2] Frissitesek letoltese Git-rol...
git pull
IF %ERRORLEVEL% NEQ 0 (
    color 0C
    echo.
    echo [HIBA] Nem sikerult a Git lehivas! Ellenorizd az internetet vagy a konfliktusokat.
    pause
    exit /b
)

echo.
echo [2/2] Docker kontener ujraepitese es inditasa...
docker-compose up -d --build
IF %ERRORLEVEL% NEQ 0 (
    color 0C
    echo.
    echo [HIBA] A Docker nem tudta ujraepiteni a botot!
    pause
    exit /b
)

echo.
echo ========================================================
echo   KESZ! A bot frissult es ujraindult.
echo   Az ablak 5 masodperc mulva bezarul.
echo ========================================================
timeout /t 5