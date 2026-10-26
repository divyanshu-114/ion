#!/usr/bin/env python3
"""Simple HTTP server to serve sample_project files locally."""

import http.server
import socketserver
import webbrowser
import os
import sys

PORT = 8000
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logging

def main():
    os.chdir(PROJECT_DIR)

    with socketserver.TCPServer(("", PORT), QuietHandler) as httpd:
        print(f"Serving sample_project at http://localhost:{PORT}")
        print("Press Ctrl+C to stop the server")

        # Open browser automatically
        webbrowser.open(f"http://localhost:{PORT}")

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped")
            sys.exit(0)

if __name__ == "__main__":
    main()
