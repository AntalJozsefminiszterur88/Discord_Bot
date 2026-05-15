@echo off
setlocal

title UMKGL Bot Frissito
color 0A

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
if errorlevel 1 (
    color 0C
    echo.
    echo [HIBA] Nem sikerult megnyitni a projekt mappajat:
    echo %SCRIPT_DIR%
    pause
    exit /b 1
)

echo ========================================================
echo   UMKGL BOT - AUTOMATA FRISSITES ES UJRAINDITAS
echo ========================================================
echo.

where git >nul 2>nul
if errorlevel 1 (
    color 0C
    echo [HIBA] A Git nincs telepitve, vagy nincs benne a PATH valtozoban.
    pause
    exit /b 1
)

where docker >nul 2>nul
if errorlevel 1 (
    color 0C
    echo [HIBA] A Docker nincs telepitve, vagy nincs benne a PATH valtozoban.
    pause
    exit /b 1
)

if not exist ".git" (
    color 0C
    echo [HIBA] Ez a mappa nem tunik Git repozitorinak.
    pause
    exit /b 1
)

if not exist "docker-compose.yml" (
    color 0C
    echo [HIBA] Nem talalom a docker-compose.yml fajlt.
    pause
    exit /b 1
)

set "HAS_UNTRACKED="
for /f %%i in ('git ls-files --others --exclude-standard') do set "HAS_UNTRACKED=1"

git diff --quiet --ignore-submodules --
set "HAS_LOCAL_CHANGES=%ERRORLEVEL%"
git diff --cached --quiet --ignore-submodules --
set "HAS_STAGED_CHANGES=%ERRORLEVEL%"

if not "%HAS_LOCAL_CHANGES%"=="0" (
    color 0E
    echo [FIGYELMEZTETES] A repoban nem commitolt modositasaid vannak.
    echo Elobb commitold vagy stasheled oket, aztan futtasd ujra a frissitest.
    pause
    exit /b 1
)

if not "%HAS_STAGED_CHANGES%"=="0" (
    color 0E
    echo [FIGYELMEZTETES] A repoban stage-elt, de nem commitolt valtozasok vannak.
    echo Elobb commitold vagy stasheled oket, aztan futtasd ujra a frissitest.
    pause
    exit /b 1
)

if defined HAS_UNTRACKED (
    color 0E
    echo [FIGYELMEZTETES] A repoban vannak uj, nem kovetett fajlok.
    echo Ellenorizd oket, majd futtasd ujra a frissitest.
    pause
    exit /b 1
)

set "COMPOSE_IMPL="
docker compose version >nul 2>nul
if not errorlevel 1 (
    set "COMPOSE_IMPL=plugin"
)

if not defined COMPOSE_IMPL (
    docker-compose version >nul 2>nul
    if not errorlevel 1 (
        set "COMPOSE_IMPL=standalone"
    )
)

if not defined COMPOSE_IMPL (
    color 0C
    echo [HIBA] Nem talalhato sem a "docker compose", sem a "docker-compose" parancs.
    pause
    exit /b 1
)

echo [1/4] Frissitesek letoltese Git-rol...
git pull --ff-only
if errorlevel 1 (
    color 0C
    echo.
    echo [HIBA] Nem sikerult a Git frissites.
    echo Lehet, hogy a lokal es a tavoli ag eltert, vagy kezi beavatkozas kell.
    pause
    exit /b 1
)

echo.
echo [2/4] Docker halo ellenorzese...
docker network inspect umkgl-network >nul 2>nul
if errorlevel 1 (
    echo A "umkgl-network" halo nem letezett, letrehozom...
    docker network create umkgl-network >nul
    if errorlevel 1 (
        color 0C
        echo [HIBA] Nem sikerult letrehozni a szukseges Docker halot.
        pause
        exit /b 1
    )
)

echo.
echo [3/4] Kontener ujraepitese...
if /I "%COMPOSE_IMPL%"=="plugin" (
    docker compose up -d --build
) else (
    docker-compose up -d --build
)
if errorlevel 1 (
    color 0C
    echo.
    echo [HIBA] A Docker nem tudta ujraepiteni vagy elinditani a botot.
    pause
    exit /b 1
)

echo.
echo [4/4] Kesz. A bot frissult es ujraindult.
echo.
echo ========================================================
echo   A frissites sikeresen lefutott.
echo   Az ablak 5 masodperc mulva bezarul.
echo ========================================================
timeout /t 5 >nul
exit /b 0
