"""Standalone Relay storage, extracted from the existing instance-scoped board."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import json
import re
import sqlite3
ROOT = Path(__file__).resolve().parent
RUNTIMES = ('Codex', 'Claude', 'MiniMax', 'Unsloth', 'Hermes', 'Other')

KINDS = ('progress', 'question', 'blocker', 'deliverable', 'note', 'summary')

STATES = ('queued', 'running', 'blocked', 'review', 'done', 'unknown')

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS instances (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT NOT NULL,
 context TEXT NOT NULL, board_url TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 instance TEXT NOT NULL REFERENCES instances(id), id TEXT NOT NULL,
 runtime TEXT NOT NULL, label TEXT NOT NULL, model TEXT NOT NULL,
 reference TEXT NOT NULL, created TEXT NOT NULL,
 PRIMARY KEY(instance,id)
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, instance TEXT NOT NULL,
 id TEXT NOT NULL, session TEXT NOT NULL, received TEXT NOT NULL,
 observed TEXT NOT NULL, body TEXT NOT NULL,
 FOREIGN KEY(instance,session) REFERENCES sessions(instance,id),
 UNIQUE(instance,id)
);
CREATE TABLE IF NOT EXISTS messages (
 instance TEXT NOT NULL, id TEXT NOT NULL, session TEXT NOT NULL,
 created TEXT NOT NULL, text TEXT NOT NULL, question TEXT,
 status TEXT NOT NULL DEFAULT 'saved',
 FOREIGN KEY(instance,session) REFERENCES sessions(instance,id),
 UNIQUE(instance,id)
);
CREATE TABLE IF NOT EXISTS receipts (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, instance TEXT NOT NULL,
 message TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL,
 created TEXT NOT NULL,
 FOREIGN KEY(instance,message) REFERENCES messages(instance,id)
);
CREATE TABLE IF NOT EXISTS instance_history (
 instance TEXT PRIMARY KEY REFERENCES instances(id), body TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research (
 instance TEXT PRIMARY KEY REFERENCES instances(id), body TEXT NOT NULL, updated TEXT NOT NULL
);
"""

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def text(value, name, maximum=6000, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f'{name}: expected {"1" if required else "0"}..{maximum} characters')
    return value.strip()

def ident(value, name='id'):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}', value):
        raise ValueError(f'{name}: use 1..128 letters, digits, dots, colons, underscores or hyphens')
    return value

def choice(value, choices, name):
    if value not in choices:
        raise ValueError(f'{name}: expected one of {", ".join(choices)}')
    return value

def safe_link(value):
    value = text(value, 'board_url', 2000, False)
    if value and urlsplit(value).scheme not in ('http', 'https', 'codex'):
        raise ValueError('board_url: use an http, https or codex link')
    return value

def session_values(instance, data):
    if not isinstance(data, dict):
        raise ValueError('Each session must be an object')
    return (instance, ident(data.get('session'), 'session'),
            choice(data.get('runtime'), RUNTIMES, 'runtime'),
            text(data.get('label'), 'label', 160), text(data.get('model', ''), 'model', 160, False),
            text(data.get('reference', ''), 'reference', 2000, False), now())

class Board:

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def require(db, instance, session=None):
        ident(instance, 'instance')
        if not db.execute('SELECT 1 FROM instances WHERE id=?', (instance,)).fetchone():
            raise ValueError('Unknown instance')
        if session is not None:
            ident(session, 'session')
            if not db.execute('SELECT 1 FROM sessions WHERE instance=? AND id=?', (instance, session)).fetchone():
                raise ValueError('Session is not attached to this instance')

    def index(self):
        with closing(self.connect()) as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM instances ORDER BY created')]
            for row in rows:
                row['grouping'] = self.history(row['id'], db).get('grouping')
            return {'instances': rows}

    def history(self, instance, db=None):
        if db is None:
            with closing(self.connect()) as connection:
                self.require(connection, instance)
                return self.history(instance, connection)
        row = db.execute('SELECT body FROM instance_history WHERE instance=?', (instance,)).fetchone()
        return json.loads(row['body']) if row else {}

    def require_writable(self, instance):
        if self.history(instance).get('grouping', {}).get('historical'):
            raise ValueError('Historical board is read-only; dispatch is disabled')

    def mutate(self, operation, data):
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        instance = ident(data.get('instance'), 'instance')
        if operation != 'create':
            self.require_writable(instance)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if operation == 'create':
                sessions = data.get('sessions', [])
                if not isinstance(sessions, list) or len(sessions) > 100:
                    raise ValueError('sessions must be a list with at most 100 items')
                db.execute('INSERT INTO instances VALUES(?,?,?,?,?,?)', (instance, text(data.get('title'), 'title', 160), text(data.get('objective', ''), 'objective', 6000, False), text(data.get('context', ''), 'context', 16000, False), safe_link(data.get('board_url', '')), now()))
                for configured in sessions:
                    db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)', session_values(instance, configured))
                return {'instance': instance}
            self.require(db, instance)
            if operation == 'context':
                if 'title' in data:
                    db.execute('UPDATE instances SET title=? WHERE id=?', (text(data['title'], 'title', 160), instance))
                db.execute('UPDATE instances SET objective=?, context=?, board_url=? WHERE id=?', (text(data.get('objective', ''), 'objective', 6000, False), text(data.get('context', ''), 'context', 16000, False), safe_link(data.get('board_url', '')), instance))
                return {'instance': instance}
            session = ident(data.get('session'), 'session')
            if operation == 'attach':
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)', session_values(instance, data))
                return {'session': session}
            self.require(db, instance, session)
            if operation == 'ingest':
                event_id = ident(data.get('id') or str(uuid4()), 'event id')
                attached = {r['id'] for r in db.execute('SELECT id FROM sessions WHERE instance=?', (instance,))}
                topic_ids = ()
                body = self.event_body(data, session, attached, topic_ids)
                encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
                previous = db.execute('SELECT * FROM events WHERE instance=? AND id=?', (instance, event_id)).fetchone()
                if previous:
                    if previous['session'] != session or previous['body'] != encoded:
                        raise ValueError('Event ID already exists with different content')
                    return {'id': event_id, 'duplicate': True}
                received = now()
                observed = data.get('observed_at', received)
                try:
                    stamp = datetime.fromisoformat(observed.replace('Z', '+00:00'))
                    if stamp.tzinfo is None or stamp.utcoffset() is None:
                        raise ValueError()
                    if stamp.timestamp() > datetime.now(timezone.utc).timestamp() + 60:
                        raise ValueError()
                    observed = stamp.astimezone(timezone.utc).isoformat(timespec='milliseconds')
                except (ValueError, TypeError, AttributeError):
                    raise ValueError('observed_at: expected a timezone-aware ISO timestamp, not in the future') from None
                db.execute('INSERT INTO events(instance,id,session,received,observed,body) VALUES(?,?,?,?,?,?)', (instance, event_id, session, received, observed, encoded))
                return {'id': event_id, 'duplicate': False}
            if operation == 'send':
                message_id = ident(data.get('id') or str(uuid4()), 'message id')
                message = text(data.get('text'), 'text')
                question = data.get('question') or None
                if question:
                    row = db.execute('SELECT body FROM events WHERE instance=? AND id=? AND session=?', (instance, ident(question), session)).fetchone()
                    if not row or json.loads(row['body'])['kind'] != 'question':
                        raise ValueError('Question does not belong to this instance and session')
                old = db.execute('SELECT * FROM messages WHERE instance=? AND id=?', (instance, message_id)).fetchone()
                if old:
                    if (old['session'], old['text'], old['question']) != (session, message, question):
                        raise ValueError('Message ID already exists with different content')
                    return {'id': message_id, 'duplicate': True}
                db.execute('INSERT INTO messages(instance,id,session,created,text,question) VALUES(?,?,?,?,?,?)', (instance, message_id, session, now(), message, question))
                return {'id': message_id, 'status': 'saved'}
            if operation == 'receipt':
                message_id = ident(data.get('message'), 'message')
                row = db.execute('SELECT * FROM messages WHERE instance=? AND id=? AND session=?', (instance, message_id, session)).fetchone()
                if not row:
                    raise ValueError('Message does not belong to this instance and session')
                status = choice(data.get('status'), ('delivered', 'acknowledged', 'completed', 'failed'), 'status')
                note = text(data.get('note'), 'note', 2000)
                allowed = {'saved': {'delivered', 'failed'}, 'delivered': {'acknowledged', 'failed'}, 'acknowledged': {'completed', 'failed'}, 'failed': {'delivered'}, 'completed': set()}
                if status not in allowed[row['status']]:
                    raise ValueError(f"Invalid delivery transition: {row['status']} -> {status}")
                db.execute('INSERT INTO receipts(instance,message,status,note,created) VALUES(?,?,?,?,?)', (instance, message_id, status, note, now()))
                db.execute('UPDATE messages SET status=? WHERE instance=? AND id=?', (status, instance, message_id))
                return {'id': message_id, 'status': status}
            raise ValueError('Unknown operation')

    @staticmethod
    def event_body(data, session=None, attached_sessions=None, topic_ids=()):
        kind = choice(data.get('kind'), KINDS, 'kind')
        body = {'kind': kind, 'text': text(data.get('text'), 'text'), 'evidence': text(data.get('evidence', ''), 'evidence', 4000, False), 'source': text(data.get('source', 'agent report'), 'source', 500), 'status': choice(data.get('status', 'unknown'), STATES, 'status'), 'default': text(data.get('default', ''), 'default', 2000, False), 'requires_approval': data.get('requires_approval', False)}
        if not isinstance(body['requires_approval'], bool):
            raise ValueError('requires_approval must be a boolean')
        if kind == 'question' and body['requires_approval']:
            body['default'] = 'Wait for explicit approval; continue independent authorized work.'
        tasks = data.get('tasks', [])
        if not isinstance(tasks, list) or len(tasks) > 100:
            raise ValueError('tasks must be a list with at most 100 items')
        body['tasks'] = []
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError('Each task must be an object')
            body['tasks'].append({'title': text(task.get('title'), 'task title', 300), 'status': choice(task.get('status'), STATES, 'task status')})
        if 'stage5_topic_update' in data:
            raise ValueError('Workspace research updates are not part of standalone Relay')
        if kind == 'summary':
            body.update(model=text(data.get('model', ''), 'model', 160, False), effort=text(data.get('effort', ''), 'effort', 80, False))
        return body

    def inbox(self, instance, session):
        with closing(self.connect()) as db:
            self.require(db, instance, session)
            return {'messages': [dict(r) for r in db.execute("SELECT * FROM messages WHERE instance=? AND session=? AND status!='completed' ORDER BY created", (instance, session))], 'read_does_not_acknowledge': True}

    def import_board(self, instance, session, source):
        """Read only the explicitly named legacy discovery board, preserving provenance."""
        source = Path(source).resolve()
        if source == self.path.resolve():
            raise ValueError('Import source must differ from destination')
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
        count = 0
        for row in rows:
            original = row['kind']
            kind = original if original in KINDS else 'note'
            result = self.mutate('ingest', {'instance': instance, 'session': session, 'id': 'legacy-' + __import__('hashlib').sha256(str(source).encode()).hexdigest()[:16] + '-' + str(row['id']), 'kind': kind, 'text': f"{row['author']} · {original} · {row['state']}\n{row['message']}", 'source': f'Imported board: {source.name}; author identity is self-declared', 'evidence': f"{source}#event-{row['id']}\n{row['evidence']}", 'observed_at': row['created']})
            count += not result['duplicate']
        return {'imported': count, 'source_read_only': str(source)}

    def snapshot(self, instance):
        with closing(self.connect()) as db:
            self.require(db, instance)
            project = dict(db.execute('SELECT * FROM instances WHERE id=?', (instance,)).fetchone())
            sessions = [dict(r) for r in db.execute('SELECT * FROM sessions WHERE instance=? ORDER BY created', (instance,))]
            events = [dict(r) for r in db.execute('SELECT * FROM events WHERE instance=? ORDER BY observed,seq', (instance,))]
            for event in events:
                event['body'] = json.loads(event['body'])
            messages = [dict(r) for r in db.execute('SELECT * FROM messages WHERE instance=? ORDER BY created', (instance,))]
            receipts = [dict(r) for r in db.execute('SELECT * FROM receipts WHERE instance=? ORDER BY seq', (instance,))]
            return dict(database=str(self.path.resolve()), cli_path=str(ROOT / 'app.py'), readme_path=str(ROOT / 'README.md'), instance=project, sessions=sessions, events=events, messages=messages, receipts=receipts, checked_at=now(), history=self.history(instance, db), execution=None, research=None)
