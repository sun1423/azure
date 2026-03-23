FROM python:3.11-slim

WORKDIR /app

# Install Azure CLI only (no Docker daemon needed - using ACR Tasks)
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    apt-transport-https \
    gnupg \
    lsb-release \
    && curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
    && az extension add --name containerapp \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
