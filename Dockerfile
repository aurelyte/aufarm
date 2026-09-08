FROM python:3.12-slim

# Install system tools useful for CTF exploits
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget netcat-openbsd nmap iputils-ping \
    gcc python3-dev libffi-dev libssl-dev git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (core backend + CTF exploit toolkit)
COPY backend/requirements.txt /app/requirements.txt
COPY backend/requirements-exploits.txt /app/requirements-exploits.txt
RUN pip install --no-cache-dir -r requirements.txt -r requirements-exploits.txt


# Copy backend source
COPY backend/ /app/

# Copy frontend to be served statically
COPY frontend/ /frontend/

# Data directories (will be overridden by volumes in compose)
RUN mkdir -p /data /exploits /logs

# Make exploits dir world-writable so uploaded scripts can be placed there
RUN chmod 777 /exploits

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
