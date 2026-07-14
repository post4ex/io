import os
import http.server
import socketserver
import urllib.request
import urllib.error
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Router] %(message)s")

PUBLIC_PORT = int(os.getenv("PORT", "7860"))
CACHE_SERVER_URL = "http://127.0.0.1:8000"
MANAGER_SERVER_URL = "http://127.0.0.1:8080"

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        # Suppress standard logging to prevent log pollution, only log proxy destinations
        pass

    def do_proxy(self):
        path = self.path
        # Route requests starting with /api/io/ to the Python Cache Server
        if path.startswith("/api/io/") or path == "/ping" or path == "/api/io/ping":
            target_base = CACHE_SERVER_URL
        else:
            target_base = MANAGER_SERVER_URL

        target_url = f"{target_base}{path}"
        
        # Read request body if present
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Copy incoming headers
        headers = {}
        for k, v in self.headers.items():
            # Skip connection headers to let urllib/http client handle them
            if k.lower() not in ('host', 'connection', 'content-length'):
                headers[k] = v

        req = urllib.request.Request(
            target_url,
            data=body,
            headers=headers,
            method=self.command
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                self.send_response(response.status)
                # Forward response headers
                for k, v in response.getheaders():
                    if k.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                        self.send_header(k, v)
                
                resp_body = response.read()
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                    self.send_header(k, v)
            resp_body = e.read()
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            logging.error(f"Error proxying {self.command} {path} to {target_url}: {e}")
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Bad Gateway: {e}".encode('utf-8'))

    def do_GET(self):
        self.do_proxy()

    def do_POST(self):
        self.do_proxy()

    def do_PUT(self):
        self.do_proxy()

    def do_DELETE(self):
        self.do_proxy()

    def do_PATCH(self):
        self.do_proxy()

    def do_OPTIONS(self):
        self.do_proxy()

    def do_HEAD(self):
        self.do_proxy()

def run():
    server_address = ('0.0.0.0', PUBLIC_PORT)
    httpd = ThreadedHTTPServer(server_address, ProxyHandler)
    logging.info(f"Proxy Router running on public port {PUBLIC_PORT}...")
    logging.info(f"  - Routing /api/io/* and /ping to {CACHE_SERVER_URL}")
    logging.info(f"  - Routing all other traffic to local Manager.io at {MANAGER_SERVER_URL}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

if __name__ == "__main__":
    run()
