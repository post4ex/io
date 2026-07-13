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
DATA_PATH     = os.getenv("DATA_PATH", "/mnt/bucket/Managerio/Businesses")
ROOT_DIR      = os.path.dirname(DATA_PATH)
APP_URL       = os.getenv("APP_URL", "https://post4ex-app.hf.space").rstrip("/")
TRIGGER_URL   = f"{APP_URL}/api/manager/sync/trigger"
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "1.5"))

# ---------------------------------------------------------------------------
# KNOWN FINANCIAL DOCUMENT GUIDs
# These match GUID_TO_DOC_TYPE in the App's cache.py.
# Only changes involving these GUIDs will trigger a webhook.
# All other GUIDs (Users, Settings, Trash, etc.) are silently ignored.
# ---------------------------------------------------------------------------
FINANCIAL_GUIDS = {
    "ad12b60b-23bf-4421-94df-8be79cef533e": "Sales Invoice",
    "0dbdbf8a-d80c-48e6-b453-bb7862445b7c": "Purchase Invoice",
    "7662b887-c8d8-486e-98fd-f9dbcd41c6dc": "Payment / Receipt",
    "6c564f4c-380c-432e-af3b-2d6514c1891c": "Journal Entry",
    "b01b1a8a-36a1-4cef-b9aa-37ab14a4f51a": "Credit Note",
    "245e5943-0092-409d-96ae-e2ee10eac75b": "Credit Note",     # Real Credit Note GUID
    "bf2a5d2a-b3dc-4898-a3d5-c9db3d66ce35": "Debit Note",
    "274fc6d0-2eac-43d0-8286-79c856e644aa": "Debit Note",      # Real Debit Note GUID
    "4a8e8ade-9b4e-4d47-8e3b-5b4e2e6f6f8a": "Expense Claim",
    "02572e0c-0167-4dbd-a392-08d8f67f3fe4": "Expense Claim",   # Real Expense Claim GUID
    "7ae97c09-de49-4f67-b4b5-d6bcbb8e6c62": "Payslip",
    "1d103fa7-6fc1-4951-811e-972968b842cc": "Payslip",          # Real Payslip GUID
}

# In-memory checkpoints: { branch: last_processed_ticks }
checkpoints   = {}
checkpoint_dir = "/app"


def ticks_to_datetime(ticks):
    try:
        from datetime import datetime, timezone
        unix_secs = (int(ticks) - 621355968000000000) / 10000000.0
        return datetime.fromtimestamp(unix_secs, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception:
        return str(ticks)


def load_checkpoint(branch):
    path = os.path.join(checkpoint_dir, f"checkpoint_{branch}.txt")
    if os.path.exists(path):
        try:
            with open(path) as f:
                val = int(f.read().strip())
            logging.info(f"Loaded checkpoint for branch '{branch}': {ticks_to_datetime(val)}")
            return val
        except Exception as e:
            logging.error(f"Failed to load checkpoint for '{branch}': {e}")
    return 0


def save_checkpoint(branch, ticks):
    path = os.path.join(checkpoint_dir, f"checkpoint_{branch}.txt")
    try:
        os.makedirs(checkpoint_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(str(ticks))
        logging.info(f"Saved checkpoint for branch '{branch}': {ticks_to_datetime(ticks)}")
    except Exception as e:
        logging.error(f"Failed to save checkpoint for '{branch}': {e}")


def extract_branch(filename):
    """
    Extract branch code from database filename.
    Convention: {BRANCH}_BRANCH.manager  →  DDN_BRANCH.manager  →  DDN
    Fallback: use stem of filename as branch.
    """
    stem = filename.replace(".manager", "")
    if "_BRANCH" in stem:
        return stem.split("_BRANCH")[0].upper()
    return stem.upper()


def check_database_changes(filename):
    branch   = extract_branch(filename)
    db_path  = os.path.join(DATA_PATH, filename)
    sync_key = os.getenv("IO_SYNC_KEY", "")

    # Load checkpoint
    if branch not in checkpoints:
        checkpoints[branch] = load_checkpoint(branch)
    last_ticks = checkpoints[branch]

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # First run: initialize checkpoint to db tip — don't sync history
        if last_ticks == 0:
            cursor.execute("SELECT MAX(Timestamp) FROM Changes")
            max_val = cursor.fetchone()[0]
            last_ticks = max_val if max_val else 0
            checkpoints[branch] = last_ticks
            save_checkpoint(branch, last_ticks)
            logging.info(f"Initialized checkpoint for branch '{branch}' to current tip: {ticks_to_datetime(last_ticks)}")
            return

        # Fetch all new change records since checkpoint
        cursor.execute("""
            SELECT Object, Timestamp, ContentTypeBefore, ContentTypeAfter
            FROM Changes
            WHERE Timestamp > ?
            ORDER BY Timestamp ASC
        """, (last_ticks,))
        rows = cursor.fetchall()

        if not rows:
            return

        logging.info(f"Found {len(rows)} new change(s) in '{filename}' — filtering for financial documents...")

        changes      = []
        highest_ticks = last_ticks

        for obj, ts, ct_before, ct_after in rows:
            highest_ticks = max(highest_ticks, ts)

            is_delete = (ct_after == "00000000-0000-0000-0000-000000000000")
            relevant_guid = ct_before if is_delete else ct_after

            if relevant_guid not in FINANCIAL_GUIDS:
                logging.info(f"  - Skipping non-financial change: GUID={relevant_guid} key={obj}")
                continue

            action = "delete" if is_delete else "upsert"
            doc_label = FINANCIAL_GUIDS[relevant_guid]
            logging.info(f"  - Detected {action.upper()}: {doc_label} | key={obj}")
            changes.append({
                "action": action,
                "key":    obj,
                "type":   relevant_guid
            })

        # Always advance checkpoint so we don't re-read the same rows
        checkpoints[branch] = highest_ticks
        save_checkpoint(branch, highest_ticks)

        if not changes:
            logging.info(f"  - No financial document changes. Checkpoint advanced. No webhook sent.")
            return

        # Send webhook to App — just the detections, the App workers do the rest
        payload = {"branch": branch.lower(), "changes": changes}
        req = urllib.request.Request(
            TRIGGER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-API-Key":    sync_key
            },
            method="POST"
        )

        logging.info(f"Sending webhook to App for branch '{branch}' with {len(changes)} financial change(s)...")
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.getcode() == 200:
                logging.info(f"App acknowledged trigger for branch '{branch}'")
            else:
                logging.error(f"App returned unexpected status {response.getcode()}")

    except urllib.error.HTTPError as he:
        body = he.read().decode("utf-8", errors="ignore")
        logging.error(f"App returned HTTP {he.code} for branch '{branch}': {body}")
    except Exception as e:
        logging.error(f"Error processing '{filename}': {e}")
    finally:
        if conn:
            conn.close()


def sync_databases_on_startup():
    logging.info("Syncing databases on startup...")
    supabase_url = os.getenv("SUPABASE_URL", "https://jxcvtcjuuvrltzjajwcm.supabase.co")
    supabase_key = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp4Y3Z0Y2p1dXZybHR6amFqd2NtIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDcyMzIzNCwiZXhwIjoyMDk2Mjk5MjM0fQ.zeGjz2rrYBrB_bmXO4zY4RW8fnsWiec9BvSuXOlTdqQ")
    
    os.makedirs(ROOT_DIR, exist_ok=True)
    
    try:
        # 1. List files recursively in Supabase bucket under io-backup/
        list_url = f"{supabase_url}/storage/v1/object/list/Backups"
        req = urllib.request.Request(
            list_url,
            data=json.dumps({
                "prefix": "io-backup",
                "options": {
                    "recursive": True
                }
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            files_metadata = json.loads(resp.read().decode())
            
        sb_files = []
        for f in files_metadata:
            name = f.get("name")
            if not name or name == "test.txt":
                continue
            # Remove leading 'io-backup/' prefix if present in the returned name
            if name.startswith("io-backup/"):
                name = name[len("io-backup/"):]
            sb_files.append(name)
            
        logging.info(f"Supabase storage files found: {sb_files}")
        
        # 2. Check local files recursively
        local_files = []
        for root, dirs, files in os.walk(ROOT_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                rel_path = os.path.relpath(filepath, ROOT_DIR)
                local_files.append(rel_path)
        logging.info(f"Local files found: {local_files}")
        
        # Scenario A: Supabase has no files, but local has files -> Migrate all local files to Supabase
        if not sb_files and local_files:
            logging.info("Supabase storage is empty. Starting migration of all local files to Supabase...")
            for rel_path in local_files:
                filepath = os.path.join(ROOT_DIR, rel_path)
                logging.info(f"Uploading '{rel_path}' to Supabase...")
                with open(filepath, "rb") as f:
                    file_content = f.read()
                upload_url = f"{supabase_url}/storage/v1/object/Backups/io-backup/{rel_path}"
                req_up = urllib.request.Request(
                    upload_url,
                    data=file_content,
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/octet-stream",
                        "x-upsert": "true"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req_up, timeout=120) as resp_up:
                    logging.info(f"Uploaded '{rel_path}': {resp_up.read().decode()}")
            logging.info("Migration to Supabase completed.")
            
        # Scenario B: Supabase has files -> Restore Supabase files locally, and upload any local files not in Supabase
        elif sb_files:
            logging.info("Found database files in Supabase. Restoring them locally...")
            for rel_path in sb_files:
                local_filepath = os.path.join(ROOT_DIR, rel_path)
                os.makedirs(os.path.dirname(local_filepath), exist_ok=True)
                
                download_url = f"{supabase_url}/storage/v1/object/Backups/io-backup/{rel_path}"
                logging.info(f"Downloading '{rel_path}' from Supabase...")
                req_dl = urllib.request.Request(
                    download_url,
                    headers={"Authorization": f"Bearer {supabase_key}"},
                    method="GET"
                )
                with urllib.request.urlopen(req_dl, timeout=120) as resp_dl:
                    file_content = resp_dl.read()
                with open(local_filepath, "wb") as f:
                    f.write(file_content)
                logging.info(f"Restored '{rel_path}' locally.")
            logging.info("Database restore from Supabase completed.")

            # Check if there are local files that are NOT in Supabase and upload them
            for rel_path in local_files:
                if rel_path not in sb_files:
                    logging.info(f"Local file '{rel_path}' is missing in Supabase. Uploading...")
                    filepath = os.path.join(ROOT_DIR, rel_path)
                    try:
                        with open(filepath, "rb") as f:
                            file_content = f.read()
                        upload_url = f"{supabase_url}/storage/v1/object/Backups/io-backup/{rel_path}"
                        req_up = urllib.request.Request(
                            upload_url,
                            data=file_content,
                            headers={
                                "Authorization": f"Bearer {supabase_key}",
                                "Content-Type": "application/octet-stream",
                                "x-upsert": "true"
                            },
                            method="POST"
                        )
                        with urllib.request.urlopen(req_up, timeout=120) as resp_up:
                            logging.info(f"Uploaded missing file '{rel_path}': {resp_up.read().decode()}")
                    except Exception as e:
                        logging.error(f"Failed to upload missing file '{rel_path}': {e}")
            
        else:
            logging.info("No files found either locally or in Supabase.")
            
    except Exception as e:
        logging.error(f"Error during startup sync: {e}")


def backup_to_supabase():
    logging.info("Running periodic backup of all database files to Supabase...")
    supabase_url = os.getenv("SUPABASE_URL", "https://jxcvtcjuuvrltzjajwcm.supabase.co")
    supabase_key = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp4Y3Z0Y2p1dXZybHR6amFqd2NtIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDcyMzIzNCwiZXhwIjoyMDk2Mjk5MjM0fQ.zeGjz2rrYBrB_bmXO4zY4RW8fnsWiec9BvSuXOlTdqQ")
    
    if not os.path.exists(ROOT_DIR):
        logging.warning(f"ROOT_DIR '{ROOT_DIR}' does not exist. Skipping backup.")
        return
        
    for root, dirs, files in os.walk(ROOT_DIR):
        for file in files:
            filepath = os.path.join(root, file)
            rel_path = os.path.relpath(filepath, ROOT_DIR)
            try:
                with open(filepath, "rb") as f:
                    file_content = f.read()
                upload_url = f"{supabase_url}/storage/v1/object/Backups/io-backup/{rel_path}"
                req = urllib.request.Request(
                    upload_url,
                    data=file_content,
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/octet-stream",
                        "x-upsert": "true"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    logging.info(f"Successfully backed up '{rel_path}': {resp.read().decode()}")
            except Exception as e:
                logging.error(f"Failed to backup '{rel_path}': {e}")


def main():
    logging.info("Starting Manager.io Real-Time Watcher Daemon...")
    logging.info(f"Watching directory: {DATA_PATH}")
    logging.info(f"App Webhook endpoint: {TRIGGER_URL}")

    sync_key = os.getenv("IO_SYNC_KEY")
    if sync_key:
        logging.info(f"IO_SYNC_KEY loaded (Prefix: {sync_key[:8]}).")
    else:
        logging.error("IO_SYNC_KEY is NOT set! Webhooks will fail authentication.")

    last_mtimes = {}

    # Initial directory scan
    try:
        if os.path.exists(DATA_PATH):
            files = [f for f in os.listdir(DATA_PATH) if f.endswith(".manager")]
            logging.info(f"Found {len(files)} database file(s): {files}")
            for f in files:
                fp = os.path.join(DATA_PATH, f)
                mtime = os.path.getmtime(fp)
                branch = extract_branch(f)
                logging.info(f"  - Monitoring '{f}' as branch '{branch}' (mtime={mtime})")
        else:
            logging.warning(f"Data path '{DATA_PATH}' does not exist on startup.")
    except Exception as e:
        logging.error(f"Error during initial scan: {e}")

    last_backup_time = time.time()

    # Main watch loop
    while True:
        try:
            if not os.path.exists(DATA_PATH):
                logging.warning(f"Data path '{DATA_PATH}' missing. Waiting...")
                time.sleep(5)
                continue

            for filename in os.listdir(DATA_PATH):
                if not filename.endswith(".manager"):
                    continue

                filepath = os.path.join(DATA_PATH, filename)
                try:
                    mtime = os.path.getmtime(filepath)
                except OSError:
                    continue

                if filename not in last_mtimes:
                    last_mtimes[filename] = mtime
                    branch = extract_branch(filename)
                    if branch not in checkpoints:
                        checkpoints[branch] = load_checkpoint(branch)
                elif mtime > last_mtimes[filename]:
                    time.sleep(0.5)  # brief wait for write to complete
                    logging.info(f"Database '{filename}' modified. Checking for changes...")
                    check_database_changes(filename)
                    last_mtimes[filename] = os.path.getmtime(filepath)

            # Perform periodic backup every 5 minutes (300 seconds)
            current_time = time.time()
            if current_time - last_backup_time >= 300:
                backup_to_supabase()
                last_backup_time = current_time

        except Exception as e:
            logging.error(f"Fatal error in watch loop: {e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--restore-only":
        sync_databases_on_startup()
        sys.exit(0)
    main()
