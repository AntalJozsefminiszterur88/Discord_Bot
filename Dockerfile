# Könnyűsúlyú Python képfájl
FROM python:3.10-slim

# Munkakönyvtár beállítása
WORKDIR /app

# Rendszerszintű függőségek telepítése (FFmpeg és libopus)
RUN apt-get update && \
    apt-get install -y ffmpeg libopus-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Python függőségek másolása és telepítése
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# A forráskód másolása
COPY . .

# A bot indítása
CMD ["python", "bot.py"]