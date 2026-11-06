"""Serve the isolated, credential-free browser workspace demo on loopback."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


PAGES = {
    '/': ('Reservation demo', '<p>Follow the link to reserve a place in the sample workshop.</p>'
                              '<p><a href="/reservation">Open reservation form</a></p>'),
    '/reservation': ('Reservation details', '<form method="post" action="/confirm">'
                     '<p><label for="name">Sample attendee name</label></p>'
                     '<p><input required maxlength="120" name="name" id="name" autocomplete="off"></p>'
                     '<button type="submit">Confirm reservation</button></form>'),
    '/received': ('Reservation received', '<p>The local demo recorded your sample reservation.</p>'
                  '<p><a href="/receipt">Download sample receipt</a></p>'),
}


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/receipt':
            return self.send_body(b'Demo workshop reservation\n', 'text/plain; charset=utf-8',
                                  {'Content-Disposition': 'attachment; filename="demo-receipt.txt"'})
        if self.path not in PAGES:
            self.send_error(404)
            return
        title, body = PAGES[self.path]
        html = f'''<!doctype html><html lang="en"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
        <style>body{{font:20px system-ui;background:#f6f8fc;color:#172b44;margin:72px}}
        main{{background:white;padding:42px;max-width:700px;border:1px solid #dce3ec;border-radius:12px}}
        a{{color:#1d54bd}} button,input{{font:inherit;padding:8px 12px}}</style>
        <main><h1>{title}</h1>{body}</main></html>'''
        self.send_body(html.encode(), 'text/html; charset=utf-8')

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        if self.path != '/confirm' or not 0 < size <= 1000:
            self.send_error(400)
            return
        submitted = parse_qs(self.rfile.read(size).decode('utf-8', errors='replace'))
        if not submitted.get('name'):
            self.send_error(400)
            return
        self.send_response(303)
        self.send_header('Location', '/received')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_body(self, content, content_type, headers=None):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()
    with ThreadingHTTPServer(('127.0.0.1', args.port), DemoHandler) as server:
        print(f'Demo site running at http://127.0.0.1:{server.server_port}/', flush=True)
        server.serve_forever()
