FROM python:3.11-slim

WORKDIR /app

# Install SSH client dependencies for paramiko
RUN apt-get update && apt-get install -y \
    openssh-client \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
