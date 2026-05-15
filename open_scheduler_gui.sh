#!/bin/bash

GUI_URL="http://localhost:5050/scheduler"

echo "Opening Discord Bot scheduler GUI..."
echo "URL: $GUI_URL"
echo
echo "If the page does not load, make sure the bot is running and port 5050 is accessible."

# Linux standard command to open a URL in the default browser
if command -v xdg-open > /dev/null; then
    xdg-open "$GUI_URL"
elif command -v gnome-open > /dev/null; then
    gnome-open "$GUI_URL"
elif command -v kde-open > /dev/null; then
    kde-open "$GUI_URL"
else
    echo "Could not find a command to open the browser automatically."
    echo "Please open the URL manually: $GUI_URL"
fi
