FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# install tzdata (timezone)
RUN apt-get update && apt-get install -y tzdata

# set timezone ke Asia/Jakarta
ENV TZ=Asia/Jakarta

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
