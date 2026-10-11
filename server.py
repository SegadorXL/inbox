#!/usr/bin/env python3
"""Small authenticated LAN inbox. Standard library only."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

MAX_BODY = 32768


def content_digest(content, tags):
    tags = list(dict.fromkeys(tags))
    if tags == ['no-tags']:
        tags = []
    canonical = json.dumps({'content': content.strip(), 'tags': tags}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def valid_http_url(value):
    """Syntactic validation only: never fetch or rewrite a shared URL."""
    if any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        # Access port too: urlsplit otherwise accepts malformed/out-of-range ports.
        port = parsed.port
        return (parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)
                and not any(c in parsed.netloc for c in '<>"\\{}|^`'))
    except ValueError:
        return False


def normalize_content(content):
    """Keep standalone URLs verbatim; extract the first URL from share text.

    In prose, quotes/angle brackets delimit URLs; sentence punctuation and
    unmatched closing brackets are removed. Balanced path parentheses survive.
    No URL means the same outer-whitespace trimming as earlier versions.
    """
    content = content.strip()
    if valid_http_url(content):
        return content
    for match in re.finditer(r"https?://[^\s<>\"'“”‘’]+", content, re.IGNORECASE):
        candidate = match.group().rstrip('.,;:!?')
        while candidate:
            closing = candidate[-1]
            opening = {')': '(', ']': '[', '}': '{'}.get(closing)
            if opening and candidate.count(closing) > candidate.count(opening):
                candidate = candidate[:-1].rstrip('.,;:!?')
            else:
                break
        if valid_http_url(candidate):
            return candidate
    return content


def validate(data):
    if not isinstance(data, dict) or set(data) - {'id', 'content', 'tags'}:
        raise ValueError('expected id, content and optional tags')
    ident, content, tags = data.get('id'), data.get('content'), data.get('tags', [])
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident):
        raise ValueError('invalid id')
    if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 16384:
        raise ValueError('content must contain 1–16384 UTF-8 bytes')
    content = content.strip()
    if any(ord(c) < 32 and c not in '\n\t' for c in content.replace('\r\n', '\n')):
        raise ValueError('control characters forbidden')
    if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(t, str) or not re.fullmatch(r'[\w-]{1,32}', t) for t in tags):
        raise ValueError('tags: up to 10 words, 32 characters each, no #')
    if re.search(r'<!-- inbox:', content):
        raise ValueError('reserved inbox marker')
    content = normalize_content(content)
    # Tags stay separate; normalization precedes both hashing and storage.
    entry = '\n<!-- inbox:' + ident + ' -->\n'
    entry += ' '.join('#' + t for t in dict.fromkeys(tags or ['no-tags'])) + '\n'
    entry += content + '\n'
    return ident, content_digest(content, tags), entry.encode()


def ordered(raw, db, new_id=None):
    previous = db.execute('SELECT body, prefix FROM projection').fetchone()
    if previous:
        baseline, prefix = previous
        if not raw.startswith(baseline) or (new_id is None and raw != baseline):
            raise RuntimeError('inbox changed externally; manual reconciliation required')
        text = raw[len(baseline):].decode('utf-8')
    else:
        text = raw.decode('utf-8')
        prefix = None
    pattern = re.compile(r'(?m)^<!-- inbox:([A-Za-z0-9_-]{1,128}) -->\n')
    matches = list(pattern.finditer(text))
    if prefix is None:
        prefix = text[:matches[0].start()] if matches else text
    elif text.strip() and not matches:
        raise RuntimeError('unrecognized capture')
    seen = set()
    tag_line = re.compile(r'#[\w-]{1,32}(?: +#[\w-]{1,32})*')
    for index, match in enumerate(matches):
        ident = match[1]
        if ident in seen:
            raise RuntimeError('duplicate inbox ID; manual review required')
        seen.add(ident)
        block = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        lines = block.rstrip('\n').split('\n')
        known = ident == new_id or db.execute('SELECT 1 FROM entries WHERE id=?', (ident,)).fetchone()
        if not known and len(lines) > 1 and tag_line.fullmatch(lines[-1]):
            tags = lines.pop()
        elif lines and tag_line.fullmatch(lines[0]):
            tags = lines.pop(0)
        else:
            tags = '#no-tags'
        content = '\n'.join(lines).strip()
        tags = list(dict.fromkeys(t[1:] for t in tags.split()))
        existing = db.execute('SELECT content,tags FROM captures WHERE id=?', (ident,)).fetchone()
        if existing and existing != (content, json.dumps(tags)):
            raise RuntimeError('historical capture conflicts with SQLite')
        db.execute('INSERT OR IGNORE INTO captures VALUES (?,?,?)', (ident, content, json.dumps(tags)))
        if ident != new_id:
            db.execute('INSERT OR IGNORE INTO entries (id,digest,offset,body,done) VALUES (?,?,0,NULL,1)',
                       (ident, content_digest(content, tags)))
        db.execute('INSERT OR IGNORE INTO ordering(id) VALUES (?)', (ident,))
    groups = {}
    for content, tags in db.execute('SELECT c.content,c.tags FROM captures c JOIN ordering o ON c.id=o.id ORDER BY o.seq DESC'):
        tag = json.loads(tags)[0]
        groups.setdefault(tag, []).append(content)
    sections = ['#' + tag + '\n\n' + '\n\n'.join(groups[tag])
                for tag in sorted(groups, key=lambda t: (t.casefold(), t))]
    after = (prefix + ('\n' if sections and prefix and not prefix.endswith('\n\n') else '') + '\n\n'.join(sections) + ('\n' if sections else '')).encode()
    db.execute('DELETE FROM projection')
    db.execute('INSERT INTO projection VALUES (?,?)', (after, prefix))
    return after


def atomic_write(path, body):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)



class Store:
    def __init__(self, state, inbox):
        self.state, self.inbox = Path(state), Path(inbox)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.state / 'state.sqlite'))
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, digest TEXT, offset INTEGER, body BLOB, done INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS ordering (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE)')
        self.db.execute('CREATE TABLE IF NOT EXISTS captures (id TEXT PRIMARY KEY, content TEXT NOT NULL, tags TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS projection (body BLOB, prefix TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS rewrite (before BLOB, after BLOB, id TEXT, digest TEXT)')
        # Upgrade old databases under the same stable writer lock.
        with open(self.state / 'writer.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for table in ('entries', 'rewrite'):
                if 'created_at' not in {row[1] for row in self.db.execute('PRAGMA table_info(' + table + ')')}:
                    self.db.execute('ALTER TABLE ' + table + ' ADD COLUMN created_at TEXT')
            self.db.commit()

    def recover(self):
        # Deleting the Inbox is an explicit fresh start. Called under writer.lock.
        # Clear capture history and recovery journals together so old data cannot
        # recreate the deleted file or reserve IDs from the previous Inbox.
        if not self.inbox.exists():
            try:
                for table in ('entries', 'captures', 'ordering', 'projection', 'rewrite'):
                    self.db.execute('DELETE FROM ' + table)
                self.db.execute("DELETE FROM sqlite_sequence WHERE name='ordering'")
                self.db.commit()
                # Establish the new empty Inbox before normal journal recovery.
                # If creation fails, a retry repeats the same empty reset.
                atomic_write(self.inbox, b'')
            except BaseException:
                self.db.rollback()
                raise
        pending = self.db.execute('SELECT before, after, id, digest, created_at FROM rewrite').fetchone()
        if pending:
            before, after, ident, digest, created_at = pending
            current = self.inbox.read_bytes() if self.inbox.exists() else b''
            if current not in (before, after):
                raise RuntimeError('inbox changed during pending rewrite')
            if current != after:
                atomic_write(self.inbox, after)
            if ident is not None:
                self.db.execute('INSERT INTO entries (id,digest,offset,body,done,created_at) VALUES (?,?,0,NULL,1,?)', (ident, digest, created_at))
            self.db.execute('DELETE FROM rewrite')
            self.db.commit()
        for ident, offset, body in self.db.execute('SELECT id, offset, body FROM entries WHERE done=0').fetchall():
            fd = os.open(str(self.inbox), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                size = os.fstat(fd).st_size
                if size < offset or size > offset + len(body):
                    raise RuntimeError('inbox changed during pending write; manual recovery required')
                os.lseek(fd, offset, os.SEEK_SET)
                existing = os.read(fd, len(body))
                if existing != body[:len(existing)]:
                    raise RuntimeError('pending write conflicts with inbox; manual recovery required')
                # Resume exactly after the verified prefix. Do not use O_APPEND:
                # recovery relies on an explicit write position under the file lock.
                os.lseek(fd, offset + len(existing), os.SEEK_SET)
                remaining = memoryview(body)[len(existing):]
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError('pending write made no progress')
                    remaining = remaining[written:]
                os.fsync(fd)
                parent_fd = os.open(str(self.inbox.parent), os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(fd)
            self.db.execute('UPDATE entries SET done=1, body=NULL WHERE id=?', (ident,))
            self.db.commit()

    def capture(self, data):
        created_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        ident, digest, body = validate(data)
        with open(self.state / 'writer.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.recover()
            self.rewrite()
            row = self.db.execute('SELECT digest FROM entries WHERE id=?', (ident,)).fetchone()
            if row:
                legacy = json.dumps({'content': data['content'].strip(), 'tags': data.get('tags', [])}, ensure_ascii=False, sort_keys=True)
                # Old captures retain their original text and digest; accept exact retries.
                if row[0] not in (digest, content_digest(data['content'], data.get('tags', [])),
                                  hashlib.sha256(legacy.encode()).hexdigest()):
                    raise FileExistsError('id already used for different content')
                return {'ok': True, 'id': ident, 'duplicate': True}
            self.rewrite(body, ident, digest, created_at)
            return {'ok': True, 'id': ident, 'duplicate': False}

    def rewrite(self, body=b'', ident=None, digest=None, created_at=None):
        exists = self.inbox.exists()
        before = self.inbox.read_bytes() if exists else b''
        try:
            previous = self.db.execute('SELECT body FROM projection').fetchone()
            if previous and previous[0] != before:
                # A hand-edited inbox is authoritative. Keep its bytes as the
                # prefix and stop projecting old captures, retaining their
                # history and IDs so retries cannot resurrect deleted items.
                prefix = before.decode('utf-8')
                self.db.execute('DELETE FROM ordering')
                self.db.execute('DELETE FROM projection')
                self.db.execute('INSERT INTO projection VALUES (?,?)', (before, prefix))
            after = ordered(before + body, self.db, ident)
            if exists and before == after:
                self.db.commit()
                return
            self.db.execute('INSERT INTO rewrite (before,after,id,digest,created_at) VALUES (?,?,?,?,?)', (before, after, ident, digest, created_at))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.recover()

    def migrate(self):
        with open(self.state / 'writer.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.recover()
            self.rewrite()



class LANServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log tokens or captured content.

    def reply(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path != '/health':
            return self.reply(404, {'ok': False, 'error': 'not found'})
        self.reply(200, {'ok': True})

    def do_POST(self):
        self.connection.settimeout(10)
        if self.path != '/capture':
            return self.reply(404, {'ok': False, 'error': 'not found'})
        auth = self.headers.get('Authorization', '')
        if not hmac.compare_digest(auth.encode(), ('Bearer ' + self.server.token).encode()):
            return self.reply(401, {'ok': False, 'error': 'unauthorized'})
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit():
            return self.reply(400, {'ok': False, 'error': 'one Content-Length required'})
        size = int(lengths[0])
        if not 0 < size <= MAX_BODY:
            return self.reply(413, {'ok': False, 'error': 'body too large or empty'})
        if self.headers.get_content_type() != 'application/json':
            return self.reply(415, {'ok': False, 'error': 'application/json required'})
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('incomplete body')
            result = self.server.store.capture(json.loads(raw))
            self.reply(200, result)
        except FileExistsError as exc:
            self.reply(409, {'ok': False, 'error': str(exc)})
        except (ValueError, UnicodeError):
            self.reply(400, {'ok': False, 'error': 'invalid JSON or capture fields'})
        except (OSError, sqlite3.Error, RuntimeError):
            self.reply(503, {'ok': False, 'error': 'storage unavailable; keep queued item'})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(Path(__file__).with_name('config.json')))
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    ip = ipaddress.IPv4Address(config['host'])
    if not any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        parser.error('host must be an explicit RFC1918 LAN IPv4 address')
    state = Path(config['state_dir'])
    token = json.loads((state / 'secrets.json').read_text())['token']
    if not isinstance(token, str) or len(token) < 32 or not token.isascii():
        parser.error('invalid token')
    server = LANServer((str(ip), config['port']), Handler)
    server.token = token
    server.store = Store(state, config['inbox'])
    server.store.migrate()
    server.serve_forever()


if __name__ == '__main__':
    main()
