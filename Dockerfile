FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    wget tar git curl \
    libicu-dev libssl-dev \
    locales \
    && rm -rf /var/lib/apt/lists/*

RUN echo "en_US.UTF-8 UTF-8" > /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8

WORKDIR /app
RUN wget https://github.com/Manager-io/Manager/releases/latest/download/ManagerServer-linux-x64.tar.gz \
    && tar -xzf ManagerServer-linux-x64.tar.gz \
    && chmod +x ManagerServer \
    && rm ManagerServer-linux-x64.tar.gz

RUN mkdir -p /data

RUN cat > start.sh << 'EOF'
#!/bin/bash

# Restore from Git backup (PRIMARY)
if [ ! -z "$GITHUB_TOKEN" ] && [ ! -z "$GITHUB_REPO" ]; then
    git config --global user.email "backup@manager.io"
    git config --global user.name "Manager Backup"
    git config --global init.defaultBranch main

    echo "Attempting restore from Git..."

    # Clone directly into /data (preserves git history)
    if git clone https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git /data 2>/dev/null; then
        echo "Restored from Git backup successfully"
    else
        echo "No backup found, starting fresh"
        cd /data
        git init
        git remote add origin https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git
    fi
fi

echo "Starting Manager.io..."
cd /app
./ManagerServer --urls http://0.0.0.0:7860 --path /data &
MANAGER_PID=$!
echo "Manager.io started with PID: $MANAGER_PID"

GIT_COUNTER=0
DROPBOX_COUNTER=0

while true; do
    sleep 300

    GIT_COUNTER=$((GIT_COUNTER + 1))
    DROPBOX_COUNTER=$((DROPBOX_COUNTER + 1))

    # Git backup every 5 minutes
    if [ ! -z "$GITHUB_TOKEN" ] && [ ! -z "$GITHUB_REPO" ]; then
        echo "Creating Git backup (#$GIT_COUNTER)..."
        cd /data
        git add .
        if git commit -m "Auto backup $(date)" 2>/dev/null; then
            git push https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git main 2>/dev/null \
                && echo "Git backup pushed" \
                || echo "Git push failed"
        else
            echo "No changes to backup"
        fi
        cd /app
    fi

    # Dropbox safe copy every hour
    if [ $DROPBOX_COUNTER -ge 12 ] && [ ! -z "$DROPBOX_TOKEN" ]; then
        echo "Creating Dropbox safe copy..."
        tar -czf /tmp/manager_backup.tar.gz -C /data . 2>/dev/null
        curl -s -X POST https://content.dropboxapi.com/2/files/upload \
            --header "Authorization: Bearer $DROPBOX_TOKEN" \
            --header "Dropbox-API-Arg: {\"path\":\"/manager_backup.tar.gz\",\"mode\":\"overwrite\"}" \
            --header "Content-Type: application/octet-stream" \
            --data-binary @/tmp/manager_backup.tar.gz
        rm -f /tmp/manager_backup.tar.gz
        DROPBOX_COUNTER=0
        echo "Dropbox copy done"
    fi

    # Restart Manager if crashed
    if ! kill -0 $MANAGER_PID 2>/dev/null; then
        echo "Manager crashed, restarting..."
        cd /app
        ./ManagerServer --urls http://0.0.0.0:7860 --path /data &
        MANAGER_PID=$!
    fi
done
EOF

RUN chmod +x start.sh

EXPOSE 7860
CMD ["./start.sh"]
