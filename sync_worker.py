import os
import time
import sqlite3
import urllib.request
import urllib.parse
import json
import logging
import hashlib
from datetime import datetime, timezone

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
DB_PATH       = os.getenv("DB_PATH", "/app/cache.db")

# ---------------------------------------------------------------------------
# SUPABASE UTILITIES
# ---------------------------------------------------------------------------
def sb_req(method, table, query_params=None, body=None, prefer=None):
    supabase_url = os.getenv("SUPABASE_URL", "https://jxcvtcjuuvrltzjajwcm.supabase.co")
    supabase_key = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp4Y3Z0Y2p1dXZybHR6amFqd2NtIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDcyMzIzNCwiZXhwIjoyMDk2Mjk5MjM0fQ.zeGjz2rrYBrB_bmXO4zY4RW8fnsWiec9BvSuXOlTdqQ")
    
    url = f"{supabase_url.rstrip('/')}/rest/v1/{table}"
    if query_params:
        url += "?" + urllib.parse.urlencode(query_params)
        
    headers = {
        "Authorization": f"Bearer {supabase_key}",
        "apikey": supabase_key,
        "Content-Type": "application/json"
    }
    if prefer:
        headers["Prefer"] = prefer
        
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except Exception as e:
        logging.error(f"Supabase request failed: {method.upper()} {url} : {e}")
        return []

def sb_get_all(table):
    offset = 0
    limit = 1000
    results = []
    while True:
        batch = sb_req("GET", table, {"limit": limit, "offset": offset})
        if not batch or not isinstance(batch, list):
            break
        results.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return results

# ---------------------------------------------------------------------------
# LOCAL MANAGER.IO UTILITIES
# ---------------------------------------------------------------------------
def request_local_manager(method, path, branch):
    api_key = os.getenv(f"{branch.upper()}_MANAGER_API_KEY")
    if not api_key:
        logging.warning(f"No API key found for branch {branch} in env. Skipping request.")
        return {}
        
    url = f"http://127.0.0.1:8080/api2/{path.lstrip('/')}"
    headers = {
        "X-API-KEY": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    for attempt in range(3):
        req = urllib.request.Request(url, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == 2:
                logging.error(f"Error querying local Manager.io on {url} (attempt 3/3): {e}")
                raise e
            logging.warning(f"Error querying local Manager.io on {url} (attempt {attempt+1}/3): {e}. Retrying in 1s...")
            time.sleep(1)

# ---------------------------------------------------------------------------
# METADATA & CONFIGURATION
# ---------------------------------------------------------------------------
FINANCIAL_GUIDS = {
    "ad12b60b-23bf-4421-94df-8be79cef533e": "Sales Invoice",
    "0dbdbf8a-d80c-48e6-b453-bb7862445b7c": "Purchase Invoice",
    "7662b887-c8d8-486e-98fd-f9dbcd41c6dc": "Payment / Receipt",
    "6c564f4c-380c-432e-af3b-2d6514c1891c": "Journal Entry",
    "b01b1a8a-36a1-4cef-b9aa-37ab14a4f51a": "Credit Note",
    "245e5943-0092-409d-96ae-e2ee10eac75b": "Credit Note",
    "bf2a5d2a-b3dc-4898-a3d5-c9db3d66ce35": "Debit Note",
    "274fc6d0-2eac-43d0-8286-79c856e644aa": "Debit Note",
    "4a8e8ade-9b4e-4d47-8e3b-5b4e2e6f6f8a": "Expense Claim",
    "02572e0c-0167-4dbd-a392-08d8f67f3fe4": "Expense Claim",
    "7ae97c09-de49-4f67-b4b5-d6bcbb8e6c62": "Payslip",
    "1d103fa7-6fc1-4951-811e-972968b842cc": "Payslip",
}

_DOC_META = {
    "Debit Note": {
        "list_path":   "debit-notes?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,issueDate,reference,supplier,description,amount,Timestamp",
        "items_key":   "debitNotes",
        "date_field":  "issueDate",
        "amount_field": "amount",
        "dox_type":    "Debit Note",
        "b2b_field":   "supplier",
        "b2b_type":    "Supplier",
    },
    "Expense Claim": {
        "list_path":   "expense-claims?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,issueDate,payee,description,amount,Timestamp",
        "items_key":   "expenseClaims",
        "date_field":  "issueDate",
        "amount_field": "amount",
        "dox_type":    "Expense Claim",
        "b2b_field":   "payee",
        "b2b_type":    "",
    },
    "Payslip": {
        "list_path":   "payslips?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,issueDate,description,netPay,Timestamp",
        "items_key":   "payslips",
        "date_field":  "issueDate",
        "amount_field": "netPay",
        "dox_type":    "Payslip",
        "b2b_field":   "",
        "b2b_type":    "",
    },
    "Sales Invoice": {
        "list_path":   "sales-invoices?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,issueDate,reference,customer,description,invoiceAmount,Timestamp",
        "items_key":   "salesInvoices",
        "date_field":  "issueDate",
        "amount_field": "invoiceAmount",
        "dox_type":    "Sales Invoice",
        "b2b_field":   "customer",
        "b2b_type":    "Customer",
    },
    "Purchase Invoice": {
        "list_path":   "purchase-invoices?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,date,reference,supplier,description,invoiceAmount,Timestamp",
        "items_key":   "purchaseInvoices",
        "date_field":  "date",
        "amount_field": "invoiceAmount",
        "dox_type":    "Purchase Invoice",
        "b2b_field":   "supplier",
        "b2b_type":    "Supplier",
    },
    "Receipt": {
        "list_path":   "receipts?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,date,reference,paidBy,receivedFrom,customer,description,amount,total,Timestamp",
        "items_key":   "receipts",
        "date_field":  "date",
        "amount_field": "total",
        "dox_type":    "Receipt",
        "b2b_field":   "receivedFrom",
        "b2b_type":    "Customer",
    },
    "Payment": {
        "list_path":   "payments?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,date,reference,paidTo,receivedFrom,supplier,description,amount,total,Timestamp",
        "items_key":   "payments",
        "date_field":  "date",
        "amount_field": "total",
        "dox_type":    "Payment",
        "b2b_field":   "paidTo",
        "b2b_type":    "Supplier",
    },
    "Credit Note": {
        "list_path":   "credit-notes?sortBy=Timestamp&sortByDesc=true&fields=key,attachment,date,issueDate,reference,customer,description,amount,Timestamp",
        "items_key":   "creditNotes",
        "date_field":  "date",
        "amount_field": "amount",
        "dox_type":    "Credit Note",
        "b2b_field":   "customer",
        "b2b_type":    "Customer",
    },
}

TXN_TYPE_MAP = {
    "Sales Invoice": "Sales Invoice",
    "Purchase Invoice": "Purchase Invoice",
    "Receipt": "Receipt",
    "Payment": "Payment",
    "Credit Note": "Credit Note",
    "Debit Note": "Debit Note",
    "Expense Claim": "Expense Claim",
    "Payslip": "Payslip",
    "Journal Entry": "Journal Entry"
}

checkpoints = {}
checkpoint_dir = os.getenv("CHECKPOINT_DIR", "/app")

# ---------------------------------------------------------------------------
# DATE & TIME CONVERSION HELPERS
# ---------------------------------------------------------------------------
def _date_to_ms(date_str):
    if not date_str:
        return 0
    try:
        if "T" in date_str:
            date_str = date_str.split("T")[0]
        parts = date_str.split("-")
        dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def _parse_iso_to_ms(iso_str):
    if not iso_str:
        return 0
    try:
        dt = datetime.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def ticks_to_datetime(ticks):
    try:
        unix_secs = (int(ticks) - 621355968000000000) / 10000000.0
        return datetime.fromtimestamp(unix_secs, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception:
        return str(ticks)

# ---------------------------------------------------------------------------
# METADATA REFRESH
# ---------------------------------------------------------------------------
def refresh_local_metadata_cache():
    logging.info("Refreshing local SQLite metadata tables B2B, BRANCHES, and STAFF...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('CREATE TABLE IF NOT EXISTS "BRANCHES" ("BRANCH_CODE" TEXT PRIMARY KEY, "NAME" TEXT);')
    cursor.execute('CREATE TABLE IF NOT EXISTS "B2B" ("CODE" TEXT PRIMARY KEY, "B2B_NAME" TEXT, "BRANCH" TEXT, "B2B_TYPE" TEXT, "IO_KEY" TEXT, "id" TEXT);')
    cursor.execute('CREATE TABLE IF NOT EXISTS "STAFF" ("STAFF_CODE" TEXT PRIMARY KEY, "STAFF_NAME" TEXT, "BRANCH" TEXT, "STATUS" TEXT, "IO_KEY" TEXT, "id" TEXT);')
    conn.commit()

    branches = sb_get_all("BRANCHES")
    cursor.execute("DELETE FROM BRANCHES")
    for b in branches:
        cursor.execute("INSERT OR REPLACE INTO BRANCHES (BRANCH_CODE, NAME) VALUES (?, ?)", 
                       (b.get("BRANCH_CODE"), b.get("NAME")))
                       
    b2b = sb_get_all("B2B")
    cursor.execute("DELETE FROM B2B")
    for b in b2b:
        cursor.execute("INSERT OR REPLACE INTO B2B (CODE, B2B_NAME, BRANCH, B2B_TYPE, IO_KEY, id) VALUES (?, ?, ?, ?, ?, ?)", 
                       (b.get("CODE"), b.get("B2B_NAME"), b.get("BRANCH"), b.get("B2B_TYPE"), b.get("IO_KEY"), b.get("id")))
                       
    staff = sb_get_all("STAFF")
    cursor.execute("DELETE FROM STAFF")
    for s in staff:
        cursor.execute("INSERT OR REPLACE INTO STAFF (STAFF_CODE, STAFF_NAME, BRANCH, STATUS, IO_KEY, id) VALUES (?, ?, ?, ?, ?, ?)", 
                       (s.get("STAFF_CODE") or s.get("CODE"), s.get("STAFF_NAME") or s.get("NAME"), s.get("BRANCH"), s.get("STATUS"), s.get("IO_KEY"), s.get("id")))

    conn.commit()
    conn.close()
    logging.info("Local SQLite metadata refreshed successfully.")

def _resolve_code_and_branch(name, fallback_branch):
    if not name:
        return "", fallback_branch
    
    name_clean = name.strip()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 1. Exact match by name
    cursor.execute("SELECT CODE, BRANCH FROM B2B WHERE B2B_NAME = ?", (name_clean,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return row["CODE"], row["BRANCH"]
        
    # 2. Match by first token
    parts = name_clean.replace(";", " ").split()
    if parts:
        primary_part = parts[0].strip().upper()
        cursor.execute("SELECT CODE, BRANCH FROM B2B WHERE UPPER(CODE) = ?", (primary_part,))
        row = cursor.fetchone()
        if row:
            conn.close()
            return row["CODE"], row["BRANCH"]
            
        cursor.execute("SELECT CODE, BRANCH FROM B2B")
        b2b_list = cursor.fetchall()
        for b in b2b_list:
            code = b["CODE"].upper()
            if code and (primary_part.startswith(code) or code.startswith(primary_part)):
                conn.close()
                return b["CODE"], b["BRANCH"]
                
    conn.close()
    return "", fallback_branch

# ---------------------------------------------------------------------------
# SYNC: CASHEIO (Metadata & References)
# ---------------------------------------------------------------------------
def sync_casheio_for_branch(branch):
    logging.info(f"Syncing casheio keys for branch '{branch}'...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    items_to_upsert = []

    def add_key_item(category, identifier, key_val, metadata=None):
        uid = f"{branch.lower()}:{category.lower()}:{identifier.strip().upper()}"
        meta_str = json.dumps(metadata) if metadata else None
        items_to_upsert.append({
            "id": uid,
            "BRANCH": branch.upper(),
            "CATEGORY": category.upper(),
            "IDENTIFIER": identifier.strip(),
            "KEY_VAL": key_val,
            "METADATA": meta_str,
            "TIME_STAMP": int(time.time() * 1000)
        })

    try:
        # 1. COA
        coa_res = request_local_manager("GET", "/chart-of-accounts", branch)
        for item in coa_res.get("chartOfAccounts", []):
            if item.get("name") and item.get("key"):
                add_key_item("coa", item["name"], item["key"])
                
        bank_res = request_local_manager("GET", "/bank-and-cash-accounts", branch)
        for item in bank_res.get("bankAndCashAccounts", []):
            if item.get("name") and item.get("key"):
                add_key_item("coa", item["name"], item["key"])

        # 2. Customers
        cust_res = request_local_manager("GET", "/customers", branch)
        for cust in cust_res.get("customers", []):
            if cust.get("name") and cust.get("key"):
                add_key_item("customers", cust["name"], cust["key"])
                if cust.get("code"):
                    add_key_item("customers", cust["code"], cust["key"])

        # 3. Suppliers
        supp_res = request_local_manager("GET", "/suppliers", branch)
        for supp in supp_res.get("suppliers", []):
            if supp.get("name") and supp.get("key"):
                add_key_item("suppliers", supp["name"], supp["key"])
                if supp.get("code"):
                    add_key_item("suppliers", supp["code"], supp["key"])

        # 4. Employees
        emp_res = request_local_manager("GET", "/employees", branch)
        for emp in emp_res.get("employees", []):
            if emp.get("name") and emp.get("key"):
                add_key_item("employees", emp["name"], emp["key"])

        # 5. Tax Codes
        tc_res = request_local_manager("GET", "/tax-codes", branch)
        for tc in tc_res.get("taxCodes", []):
            if tc.get("name") and tc.get("key"):
                add_key_item("tax_codes", tc["name"], tc["key"])

        # 6. Inventory Items
        inv_res = request_local_manager("GET", "/inventory-items", branch)
        for item in inv_res.get("inventoryItems", []):
            if item.get("key"):
                if item.get("itemName"):
                    add_key_item("inventory_items", item["itemName"], item["key"])
                if item.get("itemCode"):
                    add_key_item("inventory_items", item["itemCode"], item["key"])

        # 7. Non Inventory Items Details
        non_inv_res = request_local_manager("GET", "/non-inventory-items", branch)
        for item in non_inv_res.get("nonInventoryItems", []):
            item_key = item.get("key")
            if not item_key:
                continue
            try:
                details = request_local_manager("GET", f"/non-inventory-item-form/{item_key}", branch)
                name = details.get("Name") or item.get("itemName") or ""
                code = details.get("Code") or item.get("itemCode") or ""
                tax_code = details.get("DefaultTaxCode") if details.get("HasDefaultTaxCode") else None
                unit_price = details.get("DefaultSalesUnitPrice")
                
                item_data = {
                    "name": name,
                    "code": code,
                    "default_tax_code": tax_code,
                    "default_unit_price": unit_price
                }
                if name:
                    add_key_item("non_inventory_items", name, item_key, item_data)
                if code:
                    add_key_item("non_inventory_items", code, item_key, item_data)
            except Exception:
                pass

        # Write to local SQLite CASHEIO table
        for item in items_to_upsert:
            cursor.execute("""
            INSERT OR REPLACE INTO CASHEIO (id, BRANCH, CATEGORY, IDENTIFIER, KEY_VAL, METADATA, TIME_STAMP)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (item["id"], item["BRANCH"], item["CATEGORY"], item["IDENTIFIER"], item["KEY_VAL"], item["METADATA"], item["TIME_STAMP"]))
        conn.commit()

        # Write to Supabase CASHEIO table in bulk
        if items_to_upsert:
            sb_req("POST", "CASHEIO", {"on_conflict": "id"}, items_to_upsert, prefer="resolution=merge-duplicates")

        logging.info(f"Casheio key cache updated for branch '{branch}': {len(items_to_upsert)} entries.")
    except Exception as e:
        logging.error(f"Failed to sync casheio keys for branch '{branch}': {e}")
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# SYNC: HEADER (Document Summaries)
# ---------------------------------------------------------------------------
def _compute_header_hash(row):
    raw = "".join(str(row.get(f, "") or "") for f in [
        "DOX_TYPE", "DOX_REF", "B2B", "STAFF",
        "DOX_DESCRIPTION", "AMOUNT", "DEBIT", "CREDIT",
        "ATTACHMENT", "IO_TIMESTAMP"
    ])
    return hashlib.md5(raw.encode()).hexdigest()

def sync_headers_for_branch(branch):
    logging.info(f"Syncing transaction headers for branch '{branch}'...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    active_keys = set()

    headers_to_upsert = []
    references_to_upsert = []

    for dox_type, meta in _DOC_META.items():
        page_size = 100
        skip = 0
        while True:
            path = f"{meta['list_path']}&skip={skip}&pageSize={page_size}"
            try:
                res = request_local_manager("GET", path, branch)
                items = res.get(meta["items_key"], [])
            except Exception as e:
                logging.error(f"Error fetching {dox_type} lists for branch {branch}: {e}")
                break

            if not items:
                break

            for item in items:
                date_str = item.get(meta["date_field"]) or item.get("date") or item.get("issueDate") or ""
                io_ts = _date_to_ms(date_str)
                edit_ts = _parse_iso_to_ms(item.get("timestamp"))
                attach = "Y" if item.get("attachment") else "N"
                dox_key = str(item.get("key") or "")
                
                active_keys.add(dox_key)

                # Fetch amount
                amount = 0.0
                if meta["amount_field"] in item:
                    f_val = item[meta["amount_field"]]
                    amount = float(f_val.get("value") or 0.0) if isinstance(f_val, dict) else float(f_val or 0.0)

                b2b_val = str(item.get(meta["b2b_field"]) or "") if meta["b2b_field"] else ""
                staff_val = str(item.get("payee") or item.get("employee") or "") if dox_type in ("Expense Claim", "Payslip") else ""
                
                # Split payslip debit/credit values
                debit_amt = amount
                credit_amt = 0.00
                if dox_type == "Payslip":
                    gross = float(item.get("grossPay") or 0.0)
                    contrib = float(item.get("contribution") or 0.0)
                    debit_amt = round(gross + contrib, 2)
                    credit_amt = round(amount, 2)
                elif dox_type in ("Purchase Invoice", "Payment", "Credit Note"):
                    debit_amt = 0.00
                    credit_amt = amount

                code, _ = _resolve_code_and_branch(b2b_val, branch)

                row = {
                    "DOX_KEY":         dox_key,
                    "DOX_TYPE":        dox_type,
                    "DOX_REF":         str(item.get("reference") or ""),
                    "B2B":             b2b_val,
                    "B2B_TYPE":        meta["b2b_type"],
                    "CODE":            code,
                    "BRANCH":          branch.upper(),
                    "STAFF":           staff_val,
                    "DOX_DESCRIPTION": str(item.get("description") or ""),
                    "AMOUNT":          round(amount, 2),
                    "DEBIT":           round(debit_amt, 2),
                    "CREDIT":          round(credit_amt, 2),
                    "ATTACHMENT":      attach,
                    "IO_TIMESTAMP":    io_ts,
                    "TIME_STAMP":      edit_ts or int(time.time() * 1000),
                    "EDIT_KEY":        dox_key,
                    "VIEW_KEY":        dox_key,
                    "DOX_DATE":        io_ts
                }
                row["ROW_HASH"] = _compute_header_hash(row)

                # Check row hash to avoid unnecessary writes
                cursor.execute("SELECT ROW_HASH FROM HEADER WHERE DOX_KEY = ?", (dox_key,))
                db_row = cursor.fetchone()
                if db_row and db_row[0] == row["ROW_HASH"]:
                    continue

                # Local SQLite Write
                cursor.execute("""
                INSERT OR REPLACE INTO HEADER (
                    DOX_KEY, DOX_TYPE, DOX_REF, B2B, B2B_TYPE, CODE, BRANCH, STAFF, 
                    DOX_DESCRIPTION, AMOUNT, DEBIT, CREDIT, ATTACHMENT, ROW_HASH, 
                    IO_TIMESTAMP, TIME_STAMP, EDIT_KEY, VIEW_KEY, DOX_DATE
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    row["DOX_KEY"], row["DOX_TYPE"], row["DOX_REF"], row["B2B"], row["B2B_TYPE"], row["CODE"],
                    row["BRANCH"], row["STAFF"], row["DOX_DESCRIPTION"], row["AMOUNT"], row["DEBIT"], row["CREDIT"],
                    row["ATTACHMENT"], row["ROW_HASH"], row["IO_TIMESTAMP"], row["TIME_STAMP"], row["EDIT_KEY"],
                    row["VIEW_KEY"], row["DOX_DATE"]
                ))
                conn.commit()

                headers_to_upsert.append(row)

                # Cache maximum references
                if row["DOX_REF"]:
                    ref_id = f"{branch.lower()}:references:/{meta['dox_type'].lower().replace(' ', '-')}"
                    references_to_upsert.append({
                        "id": ref_id,
                        "BRANCH": branch.upper(),
                        "CATEGORY": "REFERENCES",
                        "IDENTIFIER": f"/{meta['dox_type'].lower().replace(' ', '-')}",
                        "KEY_VAL": row["DOX_REF"],
                        "METADATA": json.dumps({"date": date_str, "issued_to": b2b_val or staff_val}),
                        "TIME_STAMP": int(time.time() * 1000)
                    })

            if len(items) < page_size:
                break
            skip += page_size
            time.sleep(0.05)

    # Perform bulk Supabase Header writes
    if headers_to_upsert:
        sb_req("POST", "HEADER", {"on_conflict": "DOX_KEY"}, headers_to_upsert, prefer="resolution=merge-duplicates")

    # Perform bulk SQLite & Supabase Reference writes
    if references_to_upsert:
        for ref in references_to_upsert:
            cursor.execute("""
            INSERT OR REPLACE INTO CASHEIO (id, BRANCH, CATEGORY, IDENTIFIER, KEY_VAL, METADATA, TIME_STAMP)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ref["id"], ref["BRANCH"], ref["CATEGORY"], ref["IDENTIFIER"], ref["KEY_VAL"], ref["METADATA"], ref["TIME_STAMP"]))
        conn.commit()
        sb_req("POST", "CASHEIO", {"on_conflict": "id"}, references_to_upsert, prefer="resolution=merge-duplicates")

    # Reconcile Orphans (deleted documents)
    cursor.execute("SELECT DOX_KEY, DOX_TYPE FROM HEADER WHERE BRANCH = ?", (branch.upper(),))
    local_headers = cursor.fetchall()
    for row_key, row_type in local_headers:
        if row_key not in active_keys:
            # Delete locally
            cursor.execute("DELETE FROM HEADER WHERE DOX_KEY = ?", (row_key,))
            cursor.execute("DELETE FROM LEDGER WHERE DOX_KEY = ?", (row_key,))
            conn.commit()
            # Delete in Supabase
            sb_req("DELETE", "HEADER", {"DOX_KEY": f"eq.{row_key}"})
            sb_req("DELETE", "LEDGER", {"DOX_KEY": f"eq.{row_key}"})
            logging.info(f"Orphan Header deleted: {row_key} ({row_type} @ {branch})")

    conn.close()

# ---------------------------------------------------------------------------
# SYNC: LEDGER (General Ledger Transactions)
# ---------------------------------------------------------------------------
def _compute_txn_hash(txn_date, txn_type, dox_ref, account, debit, credit):
    raw = f"{txn_date}|{txn_type}|{dox_ref}|{account}|{debit}|{credit}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def sync_ledger_for_branch(branch):
    logging.info(f"Syncing ledger transactions for branch '{branch}'...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Pre-load accounts key mapping from local CASHEIO
    cursor.execute("SELECT IDENTIFIER, KEY_VAL FROM CASHEIO WHERE BRANCH = ? AND CATEGORY = 'COA'", (branch.upper(),))
    account_keys = {r[0].strip().upper(): r[1] for r in cursor.fetchall()}

    # Pre-load headers map to resolve DOX_KEY from references
    cursor.execute("SELECT DOX_TYPE, DOX_REF, DOX_KEY FROM HEADER WHERE BRANCH = ?", (branch.upper(),))
    header_cache = {(r[0], r[1]): r[2] for r in cursor.fetchall()}

    # Paginate through local Manager transactions
    skip = 0
    page_size = 200
    cleared_dox_keys = set()

    fields = [
        "Date", "Transaction", "Reference", "BankOrCashAccount",
        "Customer", "Supplier", "Employee", "Description", "Item",
        "Account", "BalanceSheetAccount", "ProfitAndLossStatementAccount",
        "LineDescription", "Qty", "UnitPrice", "Project", "Division",
        "TaxCode", "TaxAmount", "Debit", "Credit", "Amount", "Timestamp"
    ]
    fields_str = ",".join(fields)

    while True:
        path = f"transactions?fields={fields_str}&skip={skip}&pageSize={page_size}&sortBy=Date&sortByDesc=true"
        try:
            res = request_local_manager("GET", path, branch)
            txns = res.get("transactions", [])
        except Exception as e:
            logging.error(f"Error fetching ledger transactions for branch {branch}: {e}")
            break

        if not txns:
            break

        ledger_batch = []
        for txn in txns:
            txn_type = txn.get("transaction")
            d_ref = txn.get("reference")
            raw_date = txn.get("date")
            txn_date = _date_to_ms(raw_date)

            debit_val = float(txn.get("debit", {}).get("value") or 0.0) if txn.get("debit") else 0.0
            credit_val = float(txn.get("credit", {}).get("value") or 0.0) if txn.get("credit") else 0.0

            # Match counterparty B2B
            resolved_code = "SYSTEM"
            resolved_branch = branch.upper()
            b2b_val = txn.get("customer") or txn.get("supplier") or None
            staff_val = txn.get("employee")

            if b2b_val:
                parsed_code = b2b_val.split(" - ")[0].strip().upper() if " - " in b2b_val else b2b_val.strip().upper()
                cursor.execute("SELECT CODE, B2B_NAME, BRANCH FROM B2B WHERE UPPER(CODE) = ? OR UPPER(B2B_NAME) = ? OR IO_KEY = ?", (parsed_code, parsed_code, parsed_code))
                matched_b2b = cursor.fetchone()
                if matched_b2b:
                    resolved_code = matched_b2b[0]
                    resolved_branch = matched_b2b[2]
                    b2b_val = matched_b2b[1]
            elif staff_val:
                staff_name = staff_val.split(" - ")[1].strip().upper() if " - " in staff_val else staff_val.strip().upper()
                cursor.execute("SELECT STAFF_CODE, STAFF_NAME, BRANCH FROM STAFF")
                all_staff = cursor.fetchall()
                matched_staff = None
                for st in all_staff:
                    st_name = st[1].upper()
                    if st_name in staff_name or staff_name in st_name or st[0] == staff_val:
                        matched_staff = st
                        break
                if matched_staff:
                    resolved_code = matched_staff[0]
                    resolved_branch = matched_staff[2]
                    staff_val = matched_staff[1]

            acct = txn.get("account") or ""
            coa_acct = txn.get("profitAndLossStatementAccount") or txn.get("balanceSheetAccount") or acct
            coa_key = account_keys.get(coa_acct.strip().upper())

            # Resolve DOX_KEY using type map and reference
            mapped_type = TXN_TYPE_MAP.get(txn_type)
            dox_key = header_cache.get((mapped_type, d_ref)) if mapped_type else None

            txn_hash = _compute_txn_hash(txn_date, txn_type, d_ref, acct, debit_val, credit_val)

            ledger_batch.append({
                "TXN_ID": txn_hash,
                "TXN_HASH": txn_hash,
                "TXN_DATE": txn_date,
                "B2B": b2b_val,
                "B2B_TYPE": "Customer" if txn.get("customer") else ("Supplier" if txn.get("supplier") else None),
                "CODE": resolved_code,
                "BRANCH": resolved_branch,
                "STAFF": staff_val,
                "STAFF_CODE": resolved_code if staff_val else None,
                "ACCOUNT": acct,
                "BALANCE_SHEET_ACCOUNT": txn.get("balanceSheetAccount"),
                "COA_ACCOUNT": coa_acct,
                "COA_KEY": coa_key,
                "TXN_TYPE": txn_type,
                "DOX_REF": d_ref,
                "DOX_KEY": dox_key,
                "EDIT_KEY": dox_key,
                "VIEW_KEY": dox_key,
                "BANK_ACCOUNT": txn.get("bankOrCashAccount"),
                "BANK_ACCOUNT_KEY": account_keys.get(str(txn.get("bankOrCashAccount")).strip().upper()) if txn.get("bankOrCashAccount") else None,
                "DESCRIPTION": txn.get("description"),
                "DIVISION": txn.get("division"),
                "INVENTORY_ITEM": txn.get("item"),
                "DESCRIPTION_LINE": txn.get("lineDescription"),
                "PROJECT": txn.get("project"),
                "QTY_PCS_WT": float(txn.get("qty") or 0.0) if txn.get("qty") else 0.0,
                "UNIT_PRICE": float(txn.get("unitPrice", {}).get("value") or 0.0) if txn.get("unitPrice") else 0.0,
                "TAX_AMT": float(txn.get("taxAmount", {}).get("value") or 0.0) if txn.get("taxAmount") else 0.0,
                "TAX_CODE": txn.get("taxCode"),
                "DEBIT": debit_val,
                "CREDIT": credit_val,
                "IO_TIMESTAMP": txn_date,
                "TIME_STAMP": int(time.time() * 1000)
            })

        if ledger_batch:
            # Group by key/references and check if they need deletion first
            for row in ledger_batch:
                dk = row.get("DOX_KEY")
                # Clean up existing rows in local cache and Supabase to prevent orphans
                if dk and dk not in cleared_dox_keys:
                    cursor.execute("DELETE FROM LEDGER WHERE DOX_KEY = ?", (dk,))
                    sb_req("DELETE", "LEDGER", {"DOX_KEY": f"eq.{dk}"})
                    cleared_dox_keys.add(dk)
                elif not dk:
                    ref_k = (row["TXN_TYPE"], row["DOX_REF"])
                    if ref_k not in cleared_dox_keys:
                        cursor.execute("DELETE FROM LEDGER WHERE TXN_TYPE = ? AND DOX_REF = ?", ref_k)
                        sb_req("DELETE", "LEDGER", {"TXN_TYPE": f"eq.{ref_k[0]}", "DOX_REF": f"eq.{ref_k[1]}"})
                        cleared_dox_keys.add(ref_k)

            # Insert batch locally
            for row in ledger_batch:
                cursor.execute("""
                INSERT OR REPLACE INTO LEDGER (
                    TXN_ID, TXN_HASH, TXN_DATE, B2B, B2B_TYPE, CODE, BRANCH, STAFF, STAFF_CODE, ACCOUNT, 
                    BALANCE_SHEET_ACCOUNT, COA_ACCOUNT, COA_KEY, TXN_TYPE, DOX_REF, DOX_KEY, EDIT_KEY, VIEW_KEY, 
                    BANK_ACCOUNT, BANK_ACCOUNT_KEY, DESCRIPTION, DIVISION, INVENTORY_ITEM, DESCRIPTION_LINE, 
                    PROJECT, QTY_PCS_WT, UNIT_PRICE, TAX_AMT, TAX_CODE, DEBIT, CREDIT, IO_TIMESTAMP, TIME_STAMP
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    row["TXN_ID"], row["TXN_HASH"], row["TXN_DATE"], row["B2B"], row["B2B_TYPE"], row["CODE"],
                    row["BRANCH"], row["STAFF"], row["STAFF_CODE"], row["ACCOUNT"], row["BALANCE_SHEET_ACCOUNT"],
                    row["COA_ACCOUNT"], row["COA_KEY"], row["TXN_TYPE"], row["DOX_REF"], row["DOX_KEY"], row["EDIT_KEY"],
                    row["VIEW_KEY"], row["BANK_ACCOUNT"], row["BANK_ACCOUNT_KEY"], row["DESCRIPTION"], row["DIVISION"],
                    row["INVENTORY_ITEM"], row["DESCRIPTION_LINE"], row["PROJECT"], row["QTY_PCS_WT"], row["UNIT_PRICE"],
                    row["TAX_AMT"], row["TAX_CODE"], row["DEBIT"], row["CREDIT"], row["IO_TIMESTAMP"], row["TIME_STAMP"]
                ))
            conn.commit()

            # Insert batch in Supabase
            sb_req("POST", "LEDGER", None, ledger_batch, prefer="resolution=merge-duplicates")

        if len(txns) < page_size:
            break
        skip += page_size
        time.sleep(0.05)

    conn.close()

# ---------------------------------------------------------------------------
# GLOBAL SYNC MANAGER
# ---------------------------------------------------------------------------
def run_full_sync_for_branch(branch):
    try:
        sync_casheio_for_branch(branch)
        sync_headers_for_branch(branch)
        sync_ledger_for_branch(branch)
        logging.info(f"Full sync completed successfully for branch '{branch}'.")
    except Exception as e:
        logging.error(f"Error during full sync of branch '{branch}': {e}")

# ---------------------------------------------------------------------------
# STARTUP SYNC & DAEMON ACTIONS
# ---------------------------------------------------------------------------
def sync_databases_on_startup():
    logging.info("Syncing databases on startup...")
    supabase_url = os.getenv("SUPABASE_URL", "https://jxcvtcjuuvrltzjajwcm.supabase.co")
    supabase_key = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp4Y3Z0Y2p1dXZybHR6amFqd2NtIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDcyMzIzNCwiZXhwIjoyMDk2Mjk5MjM0fQ.zeGjz2rrYBrB_bmXO4zY4RW8fnsWiec9BvSuXOlTdqQ")
    supabase_bucket = os.getenv("BACKUP_BUCKET_SUPABASE", "Backups")
    supabase_folder = os.getenv("IO_BACKUP_FOLDER_SUPABASE", "io-backup")
    
    os.makedirs(ROOT_DIR, exist_ok=True)
    
    try:
        list_url = f"{supabase_url}/storage/v1/object/list/{supabase_bucket}"
        req = urllib.request.Request(
            list_url,
            data=json.dumps({"prefix": supabase_folder, "options": {"recursive": True}}).encode("utf-8"),
            headers={"Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            files_metadata = json.loads(resp.read().decode())
            
        sb_files = []
        prefix_to_remove = f"{supabase_folder}/"
        for f in files_metadata:
            name = f.get("name")
            if not name or name == "test.txt" or f.get("id") is None:
                continue
            if name.startswith(prefix_to_remove):
                name = name[len(prefix_to_remove):]
            sb_files.append(name)
            
        logging.info(f"Supabase storage files found: {sb_files}")
        
        local_files = []
        for root, dirs, files in os.walk(ROOT_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                rel_path = os.path.relpath(filepath, ROOT_DIR)
                local_files.append(rel_path)
        logging.info(f"Local files found: {local_files}")
        
        if not sb_files and local_files:
            logging.info("Supabase storage is empty. Starting migration of all local files to Supabase...")
            for rel_path in local_files:
                filepath = os.path.join(ROOT_DIR, rel_path)
                with open(filepath, "rb") as f:
                    file_content = f.read()
                quoted_path = urllib.parse.quote(rel_path)
                upload_url = f"{supabase_url}/storage/v1/object/{supabase_bucket}/{supabase_folder}/{quoted_path}"
                req_up = urllib.request.Request(
                    upload_url, data=file_content,
                    headers={"Authorization": f"Bearer {supabase_key}", "Content-Type": "application/octet-stream", "x-upsert": "true"},
                    method="POST"
                )
                with urllib.request.urlopen(req_up, timeout=120) as resp_up:
                    resp_up.read()
            logging.info("Migration to Supabase completed.")
            
        elif sb_files:
            logging.info("Found database files in Supabase. Restoring them locally...")
            for rel_path in sb_files:
                local_filepath = os.path.join(ROOT_DIR, rel_path)
                os.makedirs(os.path.dirname(local_filepath), exist_ok=True)
                
                quoted_path = urllib.parse.quote(rel_path)
                download_url = f"{supabase_url}/storage/v1/object/{supabase_bucket}/{supabase_folder}/{quoted_path}"
                req_dl = urllib.request.Request(
                    download_url, headers={"Authorization": f"Bearer {supabase_key}"}, method="GET"
                )
                with urllib.request.urlopen(req_dl, timeout=120) as resp_dl:
                    file_content = resp_dl.read()
                with open(local_filepath, "wb") as f:
                    f.write(file_content)
            logging.info("Database restore from Supabase completed.")
            
        else:
            logging.info("No files found either locally or in Supabase.")
            
    except Exception as e:
        logging.error(f"Error during startup sync: {e}")

def backup_to_supabase():
    logging.info("Running periodic backup of all database files to Supabase...")
    supabase_url = os.getenv("SUPABASE_URL", "https://jxcvtcjuuvrltzjajwcm.supabase.co")
    supabase_key = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp4Y3Z0Y2p1dXZybHR6amFqd2NtIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDcyMzIzNCwiZXhwIjoyMDk2Mjk5MjM0fQ.zeGjz2rrYBrB_bmXO4zY4RW8fnsWiec9BvSuXOlTdqQ")
    supabase_bucket = os.getenv("BACKUP_BUCKET_SUPABASE", "Backups")
    supabase_folder = os.getenv("IO_BACKUP_FOLDER_SUPABASE", "io-backup")
    
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
                quoted_path = urllib.parse.quote(rel_path)
                upload_url = f"{supabase_url}/storage/v1/object/{supabase_bucket}/{supabase_folder}/{quoted_path}"
                req = urllib.request.Request(
                    upload_url, data=file_content,
                    headers={"Authorization": f"Bearer {supabase_key}", "Content-Type": "application/octet-stream", "x-upsert": "true"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp.read()
            except Exception as e:
                logging.error(f"Failed to backup '{rel_path}': {e}")

def extract_branch(filename):
    stem = filename.replace(".manager", "")
    if "_BRANCH" in stem:
        return stem.split("_BRANCH")[0].upper()
    return stem.upper()

def check_database_changes(filename):
    branch   = extract_branch(filename)
    db_path  = os.path.join(DATA_PATH, filename)
    sync_key = os.getenv("IO_SYNC_KEY", "")

    if branch not in checkpoints:
        checkpoints[branch] = load_checkpoint(branch)
    last_ticks = checkpoints[branch]

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        if last_ticks == 0:
            cursor.execute("SELECT MAX(Timestamp) FROM Changes")
            max_val = cursor.fetchone()[0]
            last_ticks = max_val if max_val else 0
            checkpoints[branch] = last_ticks
            save_checkpoint(branch, last_ticks)
            return

        cursor.execute("""
            SELECT Object, Timestamp, ContentTypeBefore, ContentTypeAfter
            FROM Changes
            WHERE Timestamp > ?
            ORDER BY Timestamp ASC
        """, (last_ticks,))
        rows = cursor.fetchall()

        if not rows:
            return

        logging.info(f"Found {len(rows)} new change(s) in '{filename}'. Processing caches...")

        changes      = []
        highest_ticks = last_ticks

        for obj, ts, ct_before, ct_after in rows:
            highest_ticks = max(highest_ticks, ts)
            is_delete = (ct_after == "00000000-0000-0000-0000-000000000000")
            relevant_guid = ct_before if is_delete else ct_after

            if relevant_guid not in FINANCIAL_GUIDS:
                continue

            action = "delete" if is_delete else "upsert"
            doc_label = FINANCIAL_GUIDS[relevant_guid]
            changes.append({
                "action": action,
                "key":    obj,
                "type":   relevant_guid
            })

        checkpoints[branch] = highest_ticks
        save_checkpoint(branch, highest_ticks)

        if not changes:
            return

        # Perform local cache rebuild and Supabase Sync
        run_full_sync_for_branch(branch)

        # Notify the main App service that the sync is completed
        payload = {"branch": branch.lower(), "changes": changes}
        req = urllib.request.Request(
            TRIGGER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-API-Key": sync_key},
            method="POST"
        )
        logging.info(f"Notifying main App that branch '{branch}' synced...")
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()

    except Exception as e:
        logging.error(f"Error processing change for '{filename}': {e}")
    finally:
        if conn:
            conn.close()

def load_checkpoint(branch):
    path = os.path.join(checkpoint_dir, f"checkpoint_{branch}.txt")
    if os.path.exists(path):
        try:
            with open(path) as f:
                val = int(f.read().strip())
            return val
        except Exception:
            pass
    return 0

def save_checkpoint(branch, ticks):
    path = os.path.join(checkpoint_dir, f"checkpoint_{branch}.txt")
    try:
        os.makedirs(checkpoint_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(str(ticks))
    except Exception as e:
        logging.error(f"Failed to save checkpoint: {e}")

def main():
    logging.info("Starting Manager.io Local Real-Time Watcher...")
    logging.info(f"Watching directory: {DATA_PATH}")

    # Step 1: Sync B2B, STAFF metadata tables from Supabase on start
    refresh_local_metadata_cache()

    # Step 2: Wait for local ManagerServer to respond
    logging.info("Waiting for local ManagerServer on 127.0.0.1:8080 to respond...")
    for _ in range(30):
        try:
            req = urllib.request.Request("http://127.0.0.1:8080/")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.getcode() == 200:
                    logging.info("Local ManagerServer detected and responding.")
                    break
        except Exception:
            pass
        time.sleep(1)

    # Step 3: Perform initial full sync for all active branches
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT BRANCH_CODE FROM BRANCHES")
    branches = [r[0] for r in cursor.fetchall()]
    conn.close()

    logging.info(f"Active branches in database: {branches}")
    for br in branches:
        run_full_sync_for_branch(br)

    last_mtimes = {}
    try:
        if os.path.exists(DATA_PATH):
            files = [f for f in os.listdir(DATA_PATH) if f.endswith(".manager")]
            for f in files:
                fp = os.path.join(DATA_PATH, f)
                last_mtimes[f] = os.path.getmtime(fp)
    except Exception as e:
        logging.error(f"Initial folder scan failed: {e}")

    last_backup_time = time.time()

    # Main Daemon loop
    while True:
        try:
            if not os.path.exists(DATA_PATH):
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
                    time.sleep(0.5)  # wait for write
                    logging.info(f"File modified: {filename}")
                    check_database_changes(filename)
                    last_mtimes[filename] = os.path.getmtime(filepath)

            # Backup to Supabase Storage every 5 mins
            current_time = time.time()
            if current_time - last_backup_time >= 300:
                backup_to_supabase()
                last_backup_time = current_time

        except Exception as e:
            logging.error(f"Watcher loop error: {e}")

        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--restore-only":
        sync_databases_on_startup()
        sys.exit(0)
    main()
