@echo off
setlocal

set "GUI_URL=http://localhost:5050/scheduler"

echo Opening Discord Bot scheduler GUI...
echo URL: %GUI_URL%
echo.
echo If the page does not load, make sure the bot is running and port 5050 is accessible.

start "" "%GUI_URL%"
