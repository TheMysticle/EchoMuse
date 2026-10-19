"""Local-only HTML preview running the shared Pipecat Python selector.

Run: python tools/tool_selector_preview/serve.py
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent/'pipecat_echomuse'))
from tool_selector import select_tools


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/':
            self.reply(200, (ROOT/'index.html').read_bytes(), 'text/html; charset=utf-8')
        else:
            self.reply(404, b'{"error":"Not found"}')

    def do_POST(self):
        if self.path != '/api/select':
            return self.reply(404, b'{"error":"Not found"}')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 262144:
                raise ValueError('Request must be between 1 byte and 256 KB.')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Request must be an object.')
            result = select_tools(data.get('text', ''), data.get('catalogue'), data.get('language', 'en'))
            self.reply(200, json.dumps(result).encode())
        except (ValueError, TypeError, AttributeError) as error:
            self.reply(400, json.dumps({'error': str(error)}).encode())

    def log_message(self, *_):
        pass  # Do not log users' sentences or catalogue contents.


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Tool selector preview: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
