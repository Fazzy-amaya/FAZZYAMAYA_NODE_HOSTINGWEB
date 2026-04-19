FROM python:3.11-slim

# Install Node.js 20.x (LTS) and necessary build tools
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy Python dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the port (Render sets PORT env var, default 5000)
EXPOSE 5000

# Start the panel with gunicorn (eventlet worker for WebSockets)
CMD gunicorn --worker-class eventlet -w 1 app:app --bind 0.0.0.0:5000
