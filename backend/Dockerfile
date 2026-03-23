FROM python:3.11-slim

WORKDIR /app

# Install Docker CLI + Azure CLI
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    apt-transport-https \
    gnupg \
    lsb-release \
    && \
    # Docker CLI
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y docker-ce-cli && \
    # Azure CLI
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
    az extension add --name containerapp && \
    # Cleanup
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
