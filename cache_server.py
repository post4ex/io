import os
import sqlite3
import json
import urllib.parse
import http.server
import socketserver
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [CacheServer] %(message)s")

PORT = 8000
DB_PATH = "/app/cache.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

class CacheServerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # suppress default logging

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        resp = json.dumps(data).encode("utf-8")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # Health check
        if path == "/ping" or path == "/api/io/ping":
            return self.send_json({"status": "pong"})

        # Authorize request using X-API-Key header
        x_api_key = self.headers.get("X-API-Key")
        expected_key = os.getenv("IO_SYNC_KEY")
        if not expected_key or x_api_key != expected_key:
            return self.send_json({"error": "Unauthorized"}, 403)

        branch = query.get("branch", [None])[0]
        if not branch:
            return self.send_json({"error": "branch parameter is required"}, 400)
        branch = branch.upper()

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            if path == "/api/io/casheiokeys":
                # Returns the compiled casheiokeys structure for a branch:
                # { "coa": {...}, "customers": {...}, "suppliers": {...}, "employees": {...}, "tax_codes": {...}, "inventory_items": {...}, "references": {...}, "non_inventory_items": {...} }
                cursor.execute("SELECT CATEGORY, IDENTIFIER, KEY_VAL, METADATA FROM CASHEIO WHERE BRANCH = ?", (branch,))
                rows = cursor.fetchall()
                
                # Format exactly as _KEYS / references in casheiokeys.py
                result = {
                    "coa": {}, "customers": {}, "suppliers": {}, "employees": {},
                    "tax_codes": {}, "inventory_items": {}, "references": {}, "non_inventory_items": {}
                }
                for r in rows:
                    cat = r["CATEGORY"].lower()
                    if cat not in result:
                        result[cat] = {}
                    
                    if cat == "references":
                        meta = json.loads(r["METADATA"]) if r["METADATA"] else {}
                        result["references"][r["IDENTIFIER"]] = {
                            "ref": r["KEY_VAL"],
                            "date": meta.get("date"),
                            "issued_to": meta.get("issued_to")
                        }
                    elif cat == "non_inventory_items":
                        meta = json.loads(r["METADATA"]) if r["METADATA"] else {}
                        result["non_inventory_items"][r["IDENTIFIER"]] = {
                            "key": r["KEY_VAL"],
                            "name": meta.get("name"),
                            "code": meta.get("code"),
                            "default_tax_code": meta.get("default_tax_code"),
                            "default_unit_price": meta.get("default_unit_price")
                        }
                    else:
                        result[cat][r["IDENTIFIER"]] = r["KEY_VAL"]
                
                return self.send_json(result)

            elif path == "/api/io/headers":
                cursor.execute("SELECT * FROM HEADER WHERE BRANCH = ?", (branch,))
                rows = [dict(r) for r in cursor.fetchall()]
                return self.send_json(rows)

            elif path == "/api/io/ledger":
                cursor.execute("SELECT * FROM LEDGER WHERE BRANCH = ?", (branch,))
                rows = [dict(r) for r in cursor.fetchall()]
                return self.send_json(rows)

            else:
                return self.send_json({"error": "Not Found"}, 404)

        except Exception as e:
            logging.error(f"Error serving GET {path}: {e}")
            return self.send_json({"error": str(e)}, 500)
        finally:
            conn.close()

def run():
    # Pre-create tables on startup in cache.db
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS "CASHEIO" (
        "id" TEXT PRIMARY KEY,
        "BRANCH" TEXT,
        "CATEGORY" TEXT,
        "IDENTIFIER" TEXT,
        "KEY_VAL" TEXT,
        "METADATA" TEXT,
        "TIME_STAMP" BIGINT
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS "HEADER" (
        "DOX_KEY" TEXT PRIMARY KEY,
        "DOX_TYPE" TEXT,
        "DOX_REF" TEXT,
        "B2B" TEXT,
        "B2B_TYPE" TEXT,
        "CODE" TEXT,
        "BRANCH" TEXT,
        "STAFF" TEXT,
        "DOX_DESCRIPTION" TEXT,
        "NARRATION" TEXT,
        "BANK_AC" TEXT,
        "AMOUNT" REAL,
        "DEBIT" REAL,
        "CREDIT" REAL,
        "ATTACHMENT" TEXT,
        "ROW_HASH" TEXT,
        "IO_TIMESTAMP" BIGINT,
        "TIME_STAMP" BIGINT,
        "EDIT_KEY" TEXT,
        "VIEW_KEY" TEXT,
        "id" TEXT,
        "DOX_DATE" BIGINT
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS "LEDGER" (
        "TXN_ID" TEXT PRIMARY KEY,
        "TXN_HASH" TEXT,
        "TXN_DATE" BIGINT,
        "B2B" TEXT,
        "B2B_TYPE" TEXT,
        "CODE" TEXT,
        "BRANCH" TEXT,
        "STAFF" TEXT,
        "STAFF_CODE" TEXT,
        "STAFF_TYPE" TEXT,
        "ROLE" TEXT,
        "ACCOUNT" TEXT,
        "BALANCE_SHEET_ACCOUNT" TEXT,
        "COA_ACCOUNT" TEXT,
        "COA_KEY" TEXT,
        "TXN_TYPE" TEXT,
        "DOX_REF" TEXT,
        "DOX_KEY" TEXT,
        "EDIT_KEY" TEXT,
        "VIEW_KEY" TEXT,
        "BANK_ACCOUNT" TEXT,
        "BANK_ACCOUNT_KEY" TEXT,
        "DESCRIPTION" TEXT,
        "DIVISION" TEXT,
        "INVENTORY_ITEM" TEXT,
        "ITEM_KEY" TEXT,
        "DESCRIPTION_LINE" TEXT,
        "PROJECT" TEXT,
        "QTY_PCS_WT" TEXT,
        "UNIT_PRICE" REAL,
        "TAX_AMT" REAL,
        "TAX_CODE" TEXT,
        "TAX_CODE_KEY" TEXT,
        "DEBIT" REAL,
        "CREDIT" REAL,
        "IO_TIMESTAMP" BIGINT,
        "TIME_STAMP" BIGINT,
        "SERVICE_CODE" TEXT,
        "PRODUCT_CODE" TEXT,
        "id" TEXT
    );
    """)
    conn.commit()
    conn.close()

    server_address = ('127.0.0.1', PORT)
    httpd = ThreadedHTTPServer(server_address, CacheServerHandler)
    logging.info(f"Cache Server running on http://127.0.0.1:{PORT}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

if __name__ == "__main__":
    run()
