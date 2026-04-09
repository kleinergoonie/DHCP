FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt pysyncobj

# Copy application source
COPY src/ ./src/
COPY raft_server.py ./

CMD ["python", "raft_server.py", "--config", "config.json"]
