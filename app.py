"""Run a local Relay board. No executors, provider clients or workspace imports."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit

from board import Board

ROOT = Path(__file__).resolve().parent
OPERATIONS = ('create', 'context', 'attach', 'ingest', 'send')


def server(board, port=4317):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value, mime='application/json; charset=utf-8'):
            payload = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, dict) else value
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def trusted(self, mutation=False):
            expected = f'127.0.0.1:{self.server.server_port}'
            origin = self.headers.get('Origin')
            return (self.headers.get('Host') == expected and
                    (not origin or origin == 'http://' + expected) and
                    (not mutation or (origin == 'http://' + expected and
                                      self.headers.get('Content-Type') == 'application/json')))

        def do_GET(self):
            if not self.trusted():
                return self.respond(403, {'error': 'Local same-origin access required'})
            parsed = urlsplit(self.path)
            try:
                if parsed.path == '/api/instances':
                    return self.respond(200, board.index())
                if parsed.path == '/api/state':
                    return self.respond(200, board.snapshot(parse_qs(parsed.query).get('instance', [''])[0]))
                assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if parsed.path not in assets:
                    return self.respond(404, {'error': 'Not found'})
                name, mime = assets[parsed.path]
                return self.respond(200, (ROOT / name).read_bytes(), mime)
            except (ValueError, OSError, sqlite3.Error) as error:
                return self.respond(400, {'error': str(error)})

        def do_POST(self):
            if not self.trusted(True):
                return self.respond(403, {'error': 'Local same-origin JSON required'})
            operation = self.path.removeprefix('/api/')
            if self.path != '/api/' + operation or operation not in OPERATIONS:
                return self.respond(404, {'error': 'Not found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 131072:
                    raise ValueError('Request must contain 1..131072 bytes')
                return self.respond(200, board.mutate(operation, json.loads(self.rfile.read(size))))
            except (ValueError, OSError, sqlite3.Error) as error:
                return self.respond(400, {'error': str(error)})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / '.data/board.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=4317)
    for operation in (*OPERATIONS, 'receipt'):
        commands.add_parser(operation).add_argument('--json', default='-')
    commands.add_parser('instances')
    commands.add_parser('snapshot').add_argument('--instance', required=True)
    inbox = commands.add_parser('inbox')
    inbox.add_argument('--instance', required=True)
    inbox.add_argument('--session', required=True)
    args = parser.parse_args()
    try:
        board = Board(args.db)
        if args.command == 'serve':
            http = server(board, args.port)
            print(f'Relay: http://127.0.0.1:{http.server_port}', flush=True)
            try:
                http.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                http.server_close()
            return 0
        if args.command == 'instances':
            result = board.index()
        elif args.command == 'snapshot':
            result = board.snapshot(args.instance)
        elif args.command == 'inbox':
            result = board.inbox(args.instance, args.session)
        else:
            raw = sys.stdin.read() if args.json == '-' else Path(args.json).read_text()
            result = board.mutate(args.command, json.loads(raw))
        print(json.dumps({'ok': True, **result}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
