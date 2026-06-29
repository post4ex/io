import os
import time
import sqlite3
import urllib.request
import json
import logging

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# Configuration from Environment
DATA_PATH = os.getenv("DATA_PATH", "/mnt/bucket/Managerio")
APP_URL = os.getenv("APP_URL", "https://post4ex-app.hf.space").rstrip("/")
TRIGGER_URL = f"{APP_URL}/api/manager/sync/trigger"
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "1.5"))

# In-memory checkpoints for each branch database: { branch_name: last_processed_ticks }
checkpoints = {}
checkpoint_dir = "/app"  # local container directory to persist checkpoints

def ticks_to_datetime(ticks):
    try:
        ticks = int(ticks)
        unix_secs = (ticks - 621355968000000000) / 10000000.0
        from datetime import datetime, timezone
        return datetime.fromtimestamp(unix_secs, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception:
        return str(ticks)

def load_checkpoint(branch):
    path = os.path.join(checkpoint_dir, f"checkpoint_{branch}.txt")
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                val = int(f.read().strip())
                logging.info(f"Loaded checkpoint for branch '{branch}': {ticks_to_datetime(val)}")
                return val
        except Exception as e:
            logging.error(f"Failed to load checkpoint file for {branch}: {e}")
    return 0

def save_checkpoint(branch, ticks):
    path = os.path.join(checkpoint_dir, f"checkpoint_{branch}.txt")
    try:
        os.makedirs(checkpoint_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(str(ticks))
        logging.info(f"Saved checkpoint for branch '{branch}': {ticks_to_datetime(ticks)}")
    except Exception as e:
        logging.error(f"Failed to save checkpoint file for {branch}: {e}")

def get_branch_api_key(branch):
    # Lookup [BRANCH]_MANAGER_API_KEY in environment variables
    # e.g., DDN_MANAGER_API_KEY
    var_name = f"{branch.upper()}_MANAGER_API_KEY"
    return os.getenv(var_name)

def check_database_changes(filename):
    # Filename format: [BRANCH]_BRANCH.manager (e.g. DDN_BRANCH.manager)
    # Extract branch code by splitting on underscore
    branch = filename.split("_")[0].upper()
    db_path = os.path.join(DATA_PATH, filename)
    
    api_key = get_branch_api_key(branch)
    if not api_key:
        logging.warning(f"Skipping database '{filename}': no API key found for branch '{branch}' in environment (Expected {branch}_MANAGER_API_KEY)")
        return

    # Load last processed ticks
    if branch not in checkpoints:
        checkpoints[branch] = load_checkpoint(branch)
        
    last_ticks = checkpoints[branch]
    
    conn = None
    try:
        # Open database in read-only mode to prevent write conflicts
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        
        # If this is the first run (checkpoint is 0), initialize checkpoint with the latest transaction timestamp
        # to avoid syncing years of historical audit logs at startup.
        if last_ticks == 0:
            cursor.execute("SELECT MAX(Timestamp) FROM Changes")
            max_val = cursor.fetchone()[0]
            last_ticks = max_val if max_val else 0
            checkpoints[branch] = last_ticks
            save_checkpoint(branch, last_ticks)
            logging.info(f"Initialized checkpoint for branch '{branch}' to current database tip: {ticks_to_datetime(last_ticks)}")
            return

        # Query all change records since the last checkpoint
        cursor.execute("""
            SELECT Object, Timestamp, ContentTypeBefore, ContentTypeAfter 
            FROM Changes 
            WHERE Timestamp > ? 
            ORDER BY Timestamp ASC
        """, (last_ticks,))
        
        rows = cursor.fetchall()
        if not rows:
            return
            
        logging.info(f"Found {len(rows)} new changes in database '{filename}'")
        
        # Package changes
        changes = []
        highest_ticks = last_ticks
        
        for obj, ts, ct_before, ct_after in rows:
            highest_ticks = max(highest_ticks, ts)
            
            # Deletion signature
            if ct_after == '00000000-0000-0000-0000-000000000000':
                changes.append({
                    "action": "delete",
                    "key": obj,
                    "type": ct_before
                })
            else:
                changes.append({
                    "action": "upsert",
                    "key": obj,
                    "type": ct_after
                })
                
        # Send Webhook to App Space
        payload = {"changes": changes}
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key
        }
        
        req = urllib.request.Request(
            TRIGGER_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method="POST"
        )
        
        logging.info(f"Sending webhook to App for branch '{branch}' with {len(changes)} changes...")
        with urllib.request.urlopen(req, timeout=10) as response:
            status = response.getcode()
            if status == 200:
                logging.info(f"App successfully processed trigger for branch '{branch}'")
                # Update checkpoint
                checkpoints[branch] = highest_ticks
                save_checkpoint(branch, highest_ticks)
            else:
                logging.error(f"App returned unexpected status {status} for branch '{branch}'")
                
    except urllib.error.HTTPError as he:
        logging.error(f"App returned error status {he.code} for branch '{branch}': {he.read().decode('utf-8', errors='ignore')}")
    except Exception as e:
        logging.error(f"Error checking changes in database '{filename}': {e}")
    finally:
        if conn:
            conn.close()

def main():
    logging.info(f"Starting Manager.io Real-Time Watcher Daemon...")
    logging.info(f"Watching directory: {DATA_PATH}")
    logging.info(f"App Webhook endpoint: {TRIGGER_URL}")
    
    # Track file modification times
    last_mtimes = {}
    
    while True:
        try:
            if not os.path.exists(DATA_PATH):
                logging.warning(f"Data path '{DATA_PATH}' does not exist yet. Waiting...")
                time.sleep(5)
                continue
                
            for filename in os.listdir(DATA_PATH):
                if filename.endswith(".manager"):
                    filepath = os.path.join(DATA_PATH, filename)
                    try:
                        mtime = os.path.getmtime(filepath)
                    except OSError:
                        continue # File might be locked/inaccessible temporarily
                        
                    if filename not in last_mtimes:
                        # Initial discovery: record modification time
                        last_mtimes[filename] = mtime
                        # Make sure checkpoints are initialized
                        branch = filename.split("_")[0].upper()
                        if branch not in checkpoints:
                            checkpoints[branch] = load_checkpoint(branch)
                    elif mtime > last_mtimes[filename]:
                        # File modified! Wait briefly for writes to complete and lock to release
                        time.sleep(0.5)
                        logging.info(f"Database file '{filename}' was modified. Checking for changes...")
                        check_database_changes(filename)
                        last_mtimes[filename] = os.path.getmtime(filepath) # update to latest mtime after check
                        
        except Exception as e:
            logging.error(f"Fatal error in main watch loop: {e}")
            
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
