#!/bin/bash
set -e

echo "===== Application Startup at $(date -u '+%Y-%m-%d %H:%M:%S') ====="

# Load unified .env if available
ENV_FILE="/home/post4ex/FASTAPI/core/.env"
[ ! -f "$ENV_FILE" ] && ENV_FILE="$(pwd)/../FASTAPI/core/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
    [ -n "$TIDB_HOST" ] && export DB_HOST="$TIDB_HOST"
    [ -n "$TIDB_PORT" ] && export DB_PORT="$TIDB_PORT"
    [ -n "$TIDB_DATABASE" ] && export DB_DATABASE="$TIDB_DATABASE"
    [ -n "$TIDB_USER" ] && export DB_USERNAME="$TIDB_USER"
    [ -n "$TIDB_PASSWORD" ] && export DB_PASSWORD="$TIDB_PASSWORD"
    echo "✓ Loaded unified environment variables from $ENV_FILE"
fi

# Tune PHP-FPM for on-demand memory saving (hibernates when idle)
for fpm_conf in /usr/local/etc/php-fpm.d/www.conf /etc/php/*/fpm/pool.d/www.conf; do
    if [ -f "$fpm_conf" ]; then
        sed -i 's/^pm = .*/pm = ondemand/' "$fpm_conf" 2>/dev/null || true
        sed -i 's/^pm.max_children = .*/pm.max_children = 5/' "$fpm_conf" 2>/dev/null || true
        sed -i 's/^pm.process_idle_timeout = .*/pm.process_idle_timeout = 10s/' "$fpm_conf" 2>/dev/null || true
        echo "✓ Tuned PHP-FPM for on-demand low memory footprint"
    fi
done

# ---------------------------------------------------------------------------
# External database (TiDB Cloud etc. via HF Space Secrets) vs embedded MariaDB
# ---------------------------------------------------------------------------
EXTERNAL_DB=0
if [ -n "${DB_HOST:-}" ] && [ "$DB_HOST" != "127.0.0.1" ] && [ "$DB_HOST" != "localhost" ]; then
    EXTERNAL_DB=1
fi

if [ "$EXTERNAL_DB" = "1" ]; then
    echo "=== External database detected: ${DB_HOST}:${DB_PORT:-4000}/${DB_DATABASE:-invoiceninja} (skipping embedded MariaDB) ==="

    export DB_CONNECTION=mysql
    export DB_TYPE=mysql
    export DB_DATABASE="${DB_DATABASE:-invoiceninja}"
    # Never use a unix socket with an external host
    export DB_SOCKET=

    # Cloud MySQL (TiDB Serverless) requires TLS; certificates are Let's Encrypt,
    # so the system CA bundle is enough to verify them.
    if [ -z "${MYSQL_ATTR_SSL_CA:-}" ]; then
        if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
            export MYSQL_ATTR_SSL_CA=/etc/ssl/certs/ca-certificates.crt
        elif [ -f /etc/ssl/cert.pem ]; then
            export MYSQL_ATTR_SSL_CA=/etc/ssl/cert.pem
        fi
    fi

    # Invoice Ninja's config/database.php ships with no PDO SSL options —
    # inject them so Laravel's PDO actually enables TLS (idempotent). Must cover
    # ALL mysql-family connections: the default 'mysql' AND the multi-tenant
    # 'db-ninja-01/02/03' connections (used by queue jobs and set as default
    # after setup) — TiDB Serverless rejects non-TLS connections outright.
    if [ -f /var/www/app/config/database.php ] && ! grep -q "MYSQL_ATTR_SSL_CA" /var/www/app/config/database.php; then
        sed -i "s/'engine'         => 'InnoDB',/&\n            'options'        => array_filter([\n                PDO::MYSQL_ATTR_SSL_CA => env('MYSQL_ATTR_SSL_CA'),\n                PDO::MYSQL_ATTR_SSL_VERIFY_SERVER_CERT => env('MYSQL_ATTR_SSL_VERIFY_SERVER_CERT'),\n            ]),/" \
            /var/www/app/config/database.php 2>/dev/null || true
        # db-ninja-01/02/03 and any other connection with empty options
        sed -i "s/'options'        => \[\],/'options'        => array_filter([\n                PDO::MYSQL_ATTR_SSL_CA => env('MYSQL_ATTR_SSL_CA'),\n                PDO::MYSQL_ATTR_SSL_VERIFY_SERVER_CERT => env('MYSQL_ATTR_SSL_VERIFY_SERVER_CERT'),\n            ]),/" \
            /var/www/app/config/database.php 2>/dev/null || true
        echo "✓ Patched config/database.php with PDO TLS options"
    fi

    # Make sure the target database exists (idempotent)
    if command -v mysql >/dev/null 2>&1 && [ -n "${DB_USERNAME:-}" ] && [ -n "${DB_PASSWORD:-}" ]; then
        mysql --host="$DB_HOST" --port="${DB_PORT:-4000}" -u "$DB_USERNAME" -p"$DB_PASSWORD" -e \
            "CREATE DATABASE IF NOT EXISTS \`$DB_DATABASE\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null || true
    fi
    echo "✓ External database ready"
else
    echo "=== 1. Preparing MariaDB Directories ==="
    mkdir -p /run/mysqld /var/run/mysqld /var/lib/mysql
    chown -R mysql:mysql /run/mysqld /var/run/mysqld /var/lib/mysql 2>/dev/null || true

    # Clear stale/broken socket files (older deploys left a self-referencing symlink here)
    rm -f /run/mysqld/mysqld.sock /var/run/mysqld/mysqld.sock /tmp/mysql.sock 2>/dev/null || true

    # Remove skip-networking so TCP port 3306 is always open
    find /etc/my.cnf /etc/my.cnf.d /etc/mysql -type f 2>/dev/null \
        | xargs -r sed -i 's/^[[:space:]]*skip-networking/#skip-networking/g' 2>/dev/null || true

    if [ ! -d "/var/lib/mysql/mysql" ]; then
        echo "Installing initial MariaDB system tables..."
        if command -v mariadb-install-db >/dev/null 2>&1; then
            mariadb-install-db --user=mysql --datadir=/var/lib/mysql 2>/dev/null || true
        elif command -v mysql_install_db >/dev/null 2>&1; then
            mysql_install_db --user=mysql --datadir=/var/lib/mysql 2>/dev/null || true
        fi
    fi

    echo "=== 2. Starting MariaDB Server ==="
    if command -v mysqld_safe >/dev/null 2>&1; then
        mysqld_safe --user=mysql --bind-address=0.0.0.0 --port=3306 --skip-networking=OFF --datadir=/var/lib/mysql &
    else
        mariadbd-safe --user=mysql --bind-address=0.0.0.0 --port=3306 --skip-networking=OFF --datadir=/var/lib/mysql &
    fi

    echo "=== 3. Waiting for MariaDB to accept TCP connections on 127.0.0.1:3306 ==="
    for i in $(seq 1 30); do
        if mysqladmin --host=127.0.0.1 --port=3306 --user=root ping --silent 2>/dev/null; then
            echo "✓ MariaDB is ready on 127.0.0.1:3306 (attempt $i)"
            break
        fi
        echo "  Waiting... ($i/30)"
        sleep 1
    done

    echo "=== 4. Setting up Database and Users ==="
    DB_SETUP_SQL="CREATE DATABASE IF NOT EXISTS invoiceninja CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'ninja'@'%' IDENTIFIED BY 'ninjapass';
GRANT ALL PRIVILEGES ON invoiceninja.* TO 'ninja'@'%';
CREATE USER IF NOT EXISTS 'ninja'@'127.0.0.1' IDENTIFIED BY 'ninjapass';
GRANT ALL PRIVILEGES ON invoiceninja.* TO 'ninja'@'127.0.0.1';
CREATE USER IF NOT EXISTS 'ninja'@'localhost' IDENTIFIED BY 'ninjapass';
GRANT ALL PRIVILEGES ON invoiceninja.* TO 'ninja'@'localhost';
FLUSH PRIVILEGES;"
    # Try TCP first; fall back to the socket (root@localhost may use unix_socket auth)
    if ! mysql --host=127.0.0.1 --port=3306 -u root -e "$DB_SETUP_SQL" 2>/dev/null; then
        mysql -u root -e "$DB_SETUP_SQL" 2>/dev/null || true
    fi
    echo "✓ Database 'invoiceninja' and user 'ninja' configured"

    echo "=== 5. Overriding Laravel DB config to force TCP (no socket) ==="
    if [ -f /var/www/app/.env ]; then
        # Force TCP, remove any socket override
        sed -i 's/^DB_SOCKET=.*/DB_SOCKET=/' /var/www/app/.env
        sed -i 's/^DB_HOST=.*/DB_HOST=127.0.0.1/' /var/www/app/.env
        sed -i 's/^DB_PORT=.*/DB_PORT=3306/' /var/www/app/.env
    fi
    # Also export these so artisan picks them up even without .env parsing
    export DB_HOST=127.0.0.1
    export DB_PORT=3306
    export DB_SOCKET=
    export DB_USERNAME=ninja
    export DB_PASSWORD=ninjapass
    export DB_DATABASE=invoiceninja
fi

echo "=== Running Initial Migrations ==="
if [ -f /var/www/app/artisan ]; then
    cd /var/www/app
    # The image ships without .env — the setup wizard reads/writes it on submit
    # (AppSetup::updateEnvironmentProperty calls file('.env')) and crashes with
    # "Failed to open stream" if missing. Create it from the example.
    if [ ! -f /var/www/app/.env ]; then
        cp /var/www/app/.env.example /var/www/app/.env 2>/dev/null || touch /var/www/app/.env
        chmod 666 /var/www/app/.env 2>/dev/null || true
        echo "✓ Created /var/www/app/.env"
    fi

    # Ensure Laravel storage framework and public storage disk exist and are writable
    mkdir -p /var/www/app/public/storage /var/www/app/storage/app/public /var/www/app/storage/framework/sessions /var/www/app/storage/framework/views /var/www/app/storage/framework/cache/data /var/www/app/bootstrap/cache 2>/dev/null || true
    mkdir -p /var/www/html/public/storage /var/www/html/storage/app/public /var/www/html/storage/framework/sessions /var/www/html/storage/framework/views /var/www/html/storage/framework/cache/data /var/www/html/bootstrap/cache 2>/dev/null || true
    chmod -R 777 /var/www/app/storage /var/www/app/public /var/www/app/bootstrap/cache /var/www/html 2>/dev/null || true
    chown -R www-data:www-data /var/www/app /var/www/html 2>/dev/null || true

    # Explicitly disable APP_DEBUG and set production mode
    export APP_DEBUG=false
    export APP_ENV=production
    for env_file in /var/www/app/.env /var/www/html/.env; do
        if [ -f "$env_file" ]; then
            grep -q "^APP_DEBUG=" "$env_file" && sed -i "s/^APP_DEBUG=.*/APP_DEBUG=false/" "$env_file" || echo "APP_DEBUG=false" >> "$env_file"
            grep -q "^APP_ENV=" "$env_file" && sed -i "s/^APP_ENV=.*/APP_ENV=production/" "$env_file" || echo "APP_ENV=production" >> "$env_file"
        fi
    done
    php artisan migrate --force 2>&1 || true
    php artisan optimize:clear 2>&1 || true
    chmod -R 777 /var/www/app/storage /var/www/html/storage 2>/dev/null || true
fi

echo "=== Configuring Nginx for Port 7860 ==="
# The invoiceninja image ships php-fpm only (FastCGI on 127.0.0.1:9000) and no
# web server — the official compose stack uses a separate nginx container. On HF
# Spaces we must serve the app from this same container, so install/start nginx
# here, listening on 7860 and proxying PHP to local php-fpm.

# Locate the app's public dir (older images use /var/www/app, current use /var/www/html)
APP_PUBLIC=/var/www/html/public
[ -f /var/www/app/public/index.php ] && APP_PUBLIC=/var/www/app/public

# Pick the nginx include dir (Debian: conf.d, Alpine: http.d) and clear defaults
NGINX_CONF_DIR=/etc/nginx/conf.d
[ -d /etc/nginx/http.d ] && NGINX_CONF_DIR=/etc/nginx/http.d
mkdir -p "$NGINX_CONF_DIR"
rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default 2>/dev/null || true
rm -f /etc/nginx/conf.d/default.conf /etc/nginx/http.d/default.conf 2>/dev/null || true

cat > "$NGINX_CONF_DIR/invoiceninja.conf" <<'NGINX'
server {
    listen 7860 default_server;
    server_name _;
    root APP_PUBLIC_PLACEHOLDER;

    index index.php;

    charset utf-8;
    client_max_body_size 10M;
    client_body_buffer_size 10M;

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    location = /favicon.ico { access_log off; log_not_found off; }
    location = /robots.txt  { access_log off; log_not_found off; }

    error_page 404 /index.php;

    location ~ \.php$ {
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
        include fastcgi_params;
    }

    location ~ /\.(?!well-known).* {
        deny all;
    }
}
NGINX
sed -i "s|APP_PUBLIC_PLACEHOLDER|$APP_PUBLIC|" "$NGINX_CONF_DIR/invoiceninja.conf"

nginx -t 2>&1 || true
nginx 2>/dev/null || true
# Add Nginx to supervisord configuration
for sup_dir in /etc/supervisor/conf.d /etc/supervisor.d /etc/supervisord.d; do
    if [ -d "$sup_dir" ]; then
        cat > "$sup_dir/nginx.conf" <<'SUPNGINX'
[program:nginx]
command=nginx -g "daemon off;"
autostart=true
autorestart=true
priority=10
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
SUPNGINX
        echo "✓ Registered nginx in supervisor ($sup_dir/nginx.conf)"
    fi
done

# Ensure Laravel storage framework and public storage disk exist and are writable
mkdir -p /var/www/app/public/storage /var/www/app/storage/app/public /var/www/app/storage/framework/sessions /var/www/app/storage/framework/views /var/www/app/storage/framework/cache/data /var/www/app/bootstrap/cache 2>/dev/null || true
mkdir -p /var/www/html/public/storage /var/www/html/storage/app/public /var/www/html/storage/framework/sessions /var/www/html/storage/framework/views /var/www/html/storage/framework/cache/data /var/www/html/bootstrap/cache 2>/dev/null || true
find /var/www/app/storage /var/www/html/storage -type d -exec chmod 777 {} + 2>/dev/null || true
find /var/www/app/storage /var/www/html/storage -type f -exec chmod 666 {} + 2>/dev/null || true
chmod -R 777 /var/www/app/storage /var/www/app/public /var/www/app/bootstrap/cache /var/www/html/storage 2>/dev/null || true
chown -R www-data:www-data /var/www/app /var/www/html 2>/dev/null || true




# Remove the supervisor shutdown eventlistener so queue worker recycles don't kill the container
find /etc -name "*.conf" 2>/dev/null | xargs -r grep -l "eventlistener:shutdown" 2>/dev/null \
    | xargs -r sed -i '/\[eventlistener:shutdown\]/,/^$/d' 2>/dev/null || true

echo "=== Launching Web Server ==="
if [ -f /app/docker/app-entrypoint.sh ]; then
    exec /app/docker/app-entrypoint.sh "$@"
elif [ -f /docker-entrypoint.sh ]; then
    exec /docker-entrypoint.sh "$@"
elif [ -f /etc/supervisord.conf ]; then
    exec /usr/bin/supervisord -c /etc/supervisord.conf
elif [ -f /etc/supervisor/supervisord.conf ]; then
    exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
else
    exec supervisord
fi
