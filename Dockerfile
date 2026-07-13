FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    wget tar curl \
    libicu-dev libssl-dev \
    locales python3 \
    && rm -rf /var/lib/apt/lists/*

RUN echo "en_US.UTF-8 UTF-8" > /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8

WORKDIR /app
RUN wget https://github.com/Manager-io/Manager/releases/latest/download/ManagerServer-linux-x64.tar.gz \
    && tar -xzf ManagerServer-linux-x64.tar.gz \
    && chmod +x ManagerServer \
    && rm ManagerServer-linux-x64.tar.gz

RUN mkdir -p /mnt/bucket
COPY sync_worker.py /app/sync_worker.py

RUN cat > start.sh << 'EOF'
#!/bin/bash

RUN_PORT="${PORT:-7860}"
DATA_PATH="/mnt/bucket/Managerio"

# Suppress verbose ASP.NET request logging — keep only warnings & errors
export Logging__LogLevel__Default=Warning
export Logging__LogLevel__Microsoft=Warning

# Ensure the data directory exists on the bucket mount
mkdir -p "$DATA_PATH"

# Verify the bucket mount is writable
if ! touch "$DATA_PATH/.write_test" 2>/dev/null; then
    echo "FATAL: $DATA_PATH is not writable — bucket mount may be missing."
    echo "Set the volume mount: hf spaces volumes set gen4u/<space> hf://buckets/gen4u/geniefiles /mnt/bucket"
    exit 1
fi
rm -f "$DATA_PATH/.write_test"

echo "Manager.io data path: $DATA_PATH"

# Synchronize database files before starting ManagerServer
echo "Restoring databases from Supabase..."
python3 /app/sync_worker.py --restore-only

echo "Starting Manager.io on port $RUN_PORT..."

cd /app
./ManagerServer --urls "http://0.0.0.0:$RUN_PORT" --path "$DATA_PATH" &
MANAGER_PID=$!
echo "Manager.io started with PID: $MANAGER_PID"

# Start SQLite changes watcher daemon
python3 /app/sync_worker.py &
WATCHER_PID=$!
echo "Sync worker started with PID: $WATCHER_PID"

# Warmup: wait for server and hit it once so HF Space health check passes quickly
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://127.0.0.1:$RUN_PORT/ 2>/dev/null; then
        echo "Manager.io is responding, health check passed"
        break
    fi
    sleep 1
done

while true; do
    sleep 30

    # Restart Manager if crashed
    if ! kill -0 $MANAGER_PID 2>/dev/null; then
        echo "Manager crashed, restarting..."
        cd /app
        ./ManagerServer --urls http://0.0.0.0:$RUN_PORT --path "$DATA_PATH" &
        MANAGER_PID=$!
        echo "Manager.io restarted with PID: $MANAGER_PID"
    fi

    # Restart Watcher if crashed
    if ! kill -0 $WATCHER_PID 2>/dev/null; then
        echo "Sync worker crashed, restarting..."
        python3 /app/sync_worker.py &
        WATCHER_PID=$!
        echo "Sync worker restarted with PID: $WATCHER_PID"
    fi
done
EOF

RUN chmod +x start.sh

EXPOSE 7860
CMD ["./start.sh"]
