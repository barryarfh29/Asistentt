FROM python:3.11-slim

# set working directory
WORKDIR /app

# biar log langsung keluar (penting buat debug di Easypanel)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# copy requirements dulu (biar cache optimal)
COPY requirements.txt .

# install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# copy semua file project
COPY . .

# jalankan bot
CMD ["python", "main.py"]
