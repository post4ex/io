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
COPY cache_server.py /app/cache_server.py
COPY router.py /app/router.py

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

# 1. Start Manager.io internally on port 8080
echo "Starting Manager.io internally on port 8080..."
cd /app
./ManagerServer --urls "http://127.0.0.1:8080" --path "$DATA_PATH" &
MANAGER_PID=$!
echo "Manager.io started with PID: $MANAGER_PID"

# 2. Start Cache Server internally on port 8000
echo "Starting Cache Server internally on port 8000..."
python3 /app/cache_server.py &
CACHE_PID=$!
echo "Cache Server started with PID: $CACHE_PID"

# 3. Start Proxy Router publicly on port $RUN_PORT
echo "Starting Proxy Router publicly on port $RUN_PORT..."
python3 /app/router.py &
ROUTER_PID=$!
echo "Proxy Router started with PID: $ROUTER_PID"

# 4. Start SQLite changes watcher daemon
echo "Starting SQLite changes watcher daemon..."
python3 /app/sync_worker.py &
WATCHER_PID=$!
echo "Sync watcher started with PID: $WATCHER_PID"

# Warmup: wait for router to respond to ping
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://127.0.0.1:$RUN_PORT/ping 2>/dev/null; then
        echo "Proxy Router is responding, health check passed"
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
        ./ManagerServer --urls "http://127.0.0.1:8080" --path "$DATA_PATH" &
        MANAGER_PID=$!
        echo "Manager.io restarted with PID: $MANAGER_PID"
    fi

    # Restart Cache Server if crashed
    if ! kill -0 $CACHE_PID 2>/dev/null; then
        echo "Cache Server crashed, restarting..."
        python3 /app/cache_server.py &
        CACHE_PID=$!
        echo "Cache Server restarted with PID: $CACHE_PID"
    fi

    # Restart Router if crashed
    if ! kill -0 $ROUTER_PID 2>/dev/null; then
        echo "Proxy Router crashed, restarting..."
        python3 /app/router.py &
        ROUTER_PID=$!
        echo "Proxy Router restarted with PID: $ROUTER_PID"
    fi

    # Restart Watcher if crashed
    if ! kill -0 $WATCHER_PID 2>/dev/null; then
        echo "Sync watcher crashed, restarting..."
        python3 /app/sync_worker.py &
        WATCHER_PID=$!
        echo "Sync watcher restarted with PID: $WATCHER_PID"
    fi
done
EOF

RUN chmod +x start.sh

EXPOSE 7860
CMD ["./start.sh"]
