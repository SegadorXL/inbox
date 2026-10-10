import io
import os
from unittest.mock import patch
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from server import Handler, Store, validate, normalize_content, content_digest


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox.md'
        self.inbox.write_text('existing content\n')
        self.store = Store(self.root / 'state', self.inbox)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_append_and_duplicate(self):
        data = {'id': 'abc', 'content': 'https://example.com/a?b=1', 'tags': ['leer']}
        self.assertFalse(self.store.capture(data)['duplicate'])
        before = self.inbox.read_bytes()
        self.assertTrue(self.store.capture(data)['duplicate'])
        self.assertEqual(before, self.inbox.read_bytes())
        self.assertTrue(before.startswith(b'existing content\n'))
        self.assertIn(b'#leer\n\nhttps://example.com/a?b=1\n', before)
        with self.assertRaises(FileExistsError):
            self.store.capture(dict(data, content='different'))

    def test_reopen_and_multiline(self):
        data = {'id': 'text', 'content': 'uno\ndos'}
        self.store.capture(data)
        other = Store(self.root / 'state', self.inbox)
        try:
            self.assertTrue(other.capture(data)['duplicate'])
        finally:
            other.db.close()

    def test_crash_recovery(self):
        ident, digest, body = validate({'id': 'crash', 'content': 'hello'})
        offset = self.inbox.stat().st_size
        self.store.db.execute('INSERT INTO entries (id,digest,offset,body,done) VALUES (?,?,?,?,0)', (ident, digest, offset, body))
        self.store.db.commit()
        with self.inbox.open('ab') as f:
            f.write(body[:7])
        self.assertTrue(self.store.capture({'id': ident, 'content': 'hello'})['duplicate'])
        self.assertIn('#no-tags\n\nhello', self.inbox.read_text())

    def test_conflict_preserves_file(self):
        ident, digest, body = validate({'id': 'crash', 'content': 'hello'})
        offset = self.inbox.stat().st_size
        self.store.db.execute('INSERT INTO entries (id,digest,offset,body,done) VALUES (?,?,?,?,0)', (ident, digest, offset, body))
        self.store.db.commit()
        with self.inbox.open('ab') as f:
            f.write(b'changed externally')
        before = self.inbox.read_bytes()
        with self.assertRaises(RuntimeError):
            self.store.recover()
        self.assertEqual(before, self.inbox.read_bytes())

    def test_recovery_at_every_byte_boundary(self):
        original = self.inbox.read_bytes()
        for count in range(40):
            data = {'id': f'partial_{count}', 'content': 'hola ñ'}
            ident, digest, body = validate(data)
            if count > len(body):
                continue
            with self.subTest(count=count):
                self.inbox.write_bytes(original + body[:count])
                self.store.db.execute('DELETE FROM entries')
                self.store.db.execute('DELETE FROM projection')
                self.store.db.execute('DELETE FROM captures')
                self.store.db.execute('DELETE FROM ordering')
                self.store.db.execute('INSERT INTO entries (id,digest,offset,body,done) VALUES (?,?,?,?,0)',
                                      (ident, digest, len(original), body))
                self.store.db.commit()
                self.store.recover()
                self.store.recover()
                self.assertTrue(self.store.capture(data)['duplicate'])
                self.assertIn('hola ñ', self.inbox.read_text())

    def test_atomic_failure_can_resume(self):
        data = {'id': 'short', 'content': 'hello'}
        original = self.inbox.read_bytes()
        with patch('server.os.replace', side_effect=OSError('interruption')):
            with self.assertRaises(OSError):
                self.store.capture(data)
        self.assertEqual(self.inbox.read_bytes(), original)
        self.assertTrue(self.store.capture(data)['duplicate'])
        self.assertIn('hello', self.inbox.read_text())

    def test_replace_failure_after_journal_and_retry(self):
        from server import atomic_write
        data = {'id': 'journal', 'content': 'hello'}
        original = self.inbox.read_bytes()
        def fail_inbox(path, body):
            if path == self.inbox:
                raise OSError('interruption')
            atomic_write(path, body)
        with patch('server.atomic_write', side_effect=fail_inbox):
            with self.assertRaises(OSError):
                self.store.capture(data)
        self.assertEqual(self.inbox.read_bytes(), original)
        self.assertTrue(self.store.capture(data)['duplicate'])
        self.assertEqual(self.inbox.read_text().count('hello'), 1)

    def test_migration_grouping_and_text(self):
        original = ('intro\n\n<!-- inbox:a -->\nhttps://a\n#z #a\n\n'
                    '<!-- inbox:b -->\nuno\ndos\n#a\n\n'
                    '<!-- inbox:c -->\nhttps://c\n#z\n')
        self.inbox.write_text(original)
        self.store.migrate()
        result = self.inbox.read_text()
        self.assertEqual(result, 'intro\n\n#a\n\nuno\ndos\n\n#z\n\nhttps://c\n\nhttps://a\n')
        self.assertFalse(list(self.root.glob('inbox.md.bak.*')))
        self.assertEqual(json.loads(self.store.db.execute("SELECT tags FROM captures WHERE id='a'").fetchone()[0]), ['z','a'])
        self.store.migrate()
        self.assertEqual(result, self.inbox.read_text())
        self.assertTrue(self.store.capture({'id':'a','content':'https://a','tags':['z','a']})['duplicate'])
        self.store.capture({'id':'d','content':'nuevo','tags':['z','a']})
        self.assertIn('#z\n\nnuevo\n\nhttps://c\n\nhttps://a', self.inbox.read_text())

    def test_post_replace_crash_and_conflict(self):
        from server import atomic_write
        data = {'id': 'committed', 'content': 'hello'}
        def replace_then_fail(path, body):
            atomic_write(path, body)
            if path == self.inbox:
                raise OSError('crash after replace')
        with patch('server.atomic_write', side_effect=replace_then_fail):
            with self.assertRaises(OSError):
                self.store.capture(data)
        self.assertTrue(self.store.capture(data)['duplicate'])
        self.assertEqual(self.inbox.read_text().count('hello'), 1)
        with patch('server.atomic_write', side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):
                self.store.capture({'id': 'fail', 'content': 'x'})
        self.assertNotIn('inbox:fail', self.inbox.read_text())

    def test_conflict_sizes_preserve_pending_and_file(self):
        ident, digest, body = validate({'id': 'pending', 'content': 'hello'})
        original = self.inbox.read_bytes()
        self.store.db.execute('INSERT INTO entries (id,digest,offset,body,done) VALUES (?,?,?,?,0)',
                              (ident, digest, len(original), body))
        self.store.db.commit()
        for contents in (original[:-1], original + b'X', original + body + b'external'):
            self.inbox.write_bytes(contents)
            with self.assertRaises(RuntimeError):
                self.store.recover()
            self.assertEqual(self.inbox.read_bytes(), contents)
            self.assertEqual(self.store.db.execute('SELECT done, body FROM entries').fetchone(), (0, body))

    def test_concurrent_processes(self):
        import subprocess
        import sys
        code = ("from server import Store; import sys; "
                "s=Store(sys.argv[1],sys.argv[2]); "
                "s.capture({'id':sys.argv[3], 'content':'text'+sys.argv[3], 'tags':['group']})")
        processes = [subprocess.Popen([sys.executable, '-c', code,
                     str(self.root / 'state'), str(self.inbox), str(i)]) for i in range(8)]
        for process in processes:
            self.assertEqual(process.wait(timeout=20), 0)
        text = self.inbox.read_text()
        for i in range(8):
            self.assertEqual(text.count('text' + str(i)), 1)
        ranks = self.store.db.execute('SELECT id FROM ordering ORDER BY seq DESC').fetchall()
        positions = [text.index('text' + ident) for (ident,) in ranks]
        self.assertEqual(positions, sorted(positions))

    def test_rewrite_external_conflict(self):
        from server import atomic_write
        def fail_inbox(path, body):
            if path == self.inbox:
                raise OSError('interruption')
            atomic_write(path, body)
        with patch('server.atomic_write', side_effect=fail_inbox):
            with self.assertRaises(OSError):
                self.store.capture({'id': 'pending', 'content': 'hello'})
        self.inbox.write_text('external edit')
        with self.assertRaises(RuntimeError):
            self.store.capture({'id': 'pending', 'content': 'hello'})
        self.assertEqual(self.inbox.read_text(), 'external edit')

    def test_legacy_literal_and_historical_duplicates(self):
        self.inbox.write_text('<!-- inbox:old -->\n#literal\ntexto ñ\n#z #z #a\n')
        self.store.migrate()
        self.assertIn('#z\n\n#literal\ntexto ñ', self.inbox.read_text())
        before = self.inbox.read_bytes()
        data = {'id': 'old', 'content': '  #literal\ntexto ñ  ', 'tags': ['z', 'a', 'z']}
        self.assertTrue(self.store.capture(data)['duplicate'])
        self.assertEqual(before, self.inbox.read_bytes())
        self.assertIsNone(self.store.db.execute('SELECT created_at FROM entries WHERE id="old"').fetchone()[0])
        with self.assertRaises(FileExistsError):
            self.store.capture(dict(data, tags=['a', 'z']))
        self.store.migrate()
        self.assertEqual(before, self.inbox.read_bytes())

    def test_historical_without_tags_and_capture_without_migrate(self):
        self.inbox.write_text('<!-- inbox:old -->\ntexto\n')
        self.assertTrue(self.store.capture({'id': 'old', 'content': 'texto'})['duplicate'])
        self.assertTrue(self.store.capture({'id': 'old', 'content': 'texto', 'tags': ['no-tags']})['duplicate'])

    def test_created_at_survives_pending_recovery(self):
        from datetime import datetime, timezone
        from server import atomic_write
        start = datetime.now(timezone.utc)
        def fail_inbox(path, body):
            if path == self.inbox:
                raise OSError('interruption')
            atomic_write(path, body)
        data = {'id': 'dated', 'content': 'hello', 'tags': ['a', 'a']}
        with patch('server.atomic_write', side_effect=fail_inbox):
            with self.assertRaises(OSError):
                self.store.capture(data)
        timestamp = self.store.db.execute('SELECT created_at FROM rewrite').fetchone()[0]
        self.assertTrue(timestamp.endswith('Z'))
        self.assertGreaterEqual(datetime.fromisoformat(timestamp.replace('Z', '+00:00')), start)
        self.assertTrue(self.store.capture(dict(data, tags=['a']))['duplicate'])
        self.assertEqual(self.store.db.execute('SELECT created_at FROM entries WHERE id="dated"').fetchone()[0], timestamp)
        self.assertNotIn(timestamp, self.inbox.read_text())

    def test_no_per_request_backups_and_external_edit(self):
        for i in range(3):
            self.store.capture({'id':str(i),'content':str(i)})
        self.assertFalse(list(self.root.glob('inbox.md.bak.*')))
        self.assertNotIn('<!-- inbox:', self.inbox.read_text())
        self.assertEqual(self.inbox.read_text().count('#no-tags'), 1)
        self.inbox.write_text(self.inbox.read_text() + 'manual edit')
        before = self.inbox.read_bytes()
        with self.assertRaises(RuntimeError):
            self.store.migrate()
        self.assertEqual(before, self.inbox.read_bytes())

    def test_upgrade_existing_schema_and_digest(self):
        import hashlib
        import sqlite3
        state = self.root / 'legacy'
        state.mkdir()
        db = sqlite3.connect(state / 'state.sqlite')
        db.execute('CREATE TABLE entries (id TEXT PRIMARY KEY, digest TEXT, offset INTEGER, body BLOB, done INTEGER)')
        db.execute('CREATE TABLE rewrite (before BLOB, after BLOB, id TEXT, digest TEXT)')
        data = {'id': 'legacy', 'content': 'hello', 'tags': ['a', 'a']}
        canonical = json.dumps({'content': 'hello', 'tags': ['a', 'a']}, ensure_ascii=False, sort_keys=True)
        db.execute('INSERT INTO entries VALUES (?,?,0,NULL,1)', ('legacy', hashlib.sha256(canonical.encode()).hexdigest()))
        db.commit()
        db.close()
        self.inbox.write_bytes(validate(data)[2])
        other = Store(state, self.inbox)
        try:
            other.migrate()
            self.assertTrue(other.capture(data)['duplicate'])
            self.assertIsNone(other.db.execute('SELECT created_at FROM entries').fetchone()[0])
        finally:
            other.db.close()

    def test_missing_inbox_starts_fresh_and_reuses_old_id(self):
        self.store.capture({'id': 'old', 'content': 'old history', 'tags': ['leer']})
        self.inbox.unlink()
        new = {'id': 'old', 'content': 'fresh content', 'tags': ['nuevo']}
        self.assertFalse(self.store.capture(new)['duplicate'])
        self.assertEqual(self.inbox.read_text(), '#nuevo\n\nfresh content\n')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM entries').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT seq FROM ordering').fetchone()[0], 1)
        self.assertTrue(self.store.capture(new)['duplicate'])

    def test_missing_inbox_startup_clears_all_history(self):
        self.store.capture({'id': 'old', 'content': 'saved'})
        self.inbox.unlink()
        self.store.migrate()
        self.assertEqual(self.inbox.read_bytes(), b'')
        for table in ('entries', 'captures', 'ordering', 'rewrite'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT body FROM projection').fetchone()[0], b'')

    def test_missing_inbox_discards_pending_old_rewrite(self):
        self.store.capture({'id': 'old', 'content': 'saved'})
        with patch('server.os.replace', side_effect=OSError('interruption')):
            with self.assertRaises(OSError):
                self.store.capture({'id': 'pending', 'content': 'pending history'})
        self.inbox.unlink()
        self.store.db.close()
        self.store = Store(self.root / 'state', self.inbox)
        self.store.migrate()
        self.assertEqual(self.inbox.read_bytes(), b'')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM rewrite').fetchone()[0], 0)
        self.assertFalse(self.store.capture({'id': 'pending', 'content': 'fresh'})['duplicate'])
        self.assertNotIn('saved', self.inbox.read_text())
        self.assertNotIn('pending history', self.inbox.read_text())

    def test_validation(self):
        for data in ({}, {'id': '../x', 'content': 'a'}, {'id': 'x', 'content': ''},
                     {'id': 'x', 'content': 'a' * 16385},
                     {'id': 'x', 'content': 'a', 'tags': ['#bad']}):
            with self.assertRaises(ValueError):
                validate(data)

    def test_shared_content_normalization(self):
        cases = [
            ('https://example.com/path?a=1&b=two%20words#part',
             'https://example.com/path?a=1&b=two%20words#part'),
            ('HTTPS://EXAMPLE.com/path?literal=.,;!', 'HTTPS://EXAMPLE.com/path?literal=.,;!'),
            ('https://example.com/wiki/Topic_(detail)', 'https://example.com/wiki/Topic_(detail)'),
            ('Mira este reel en Instagram\nhttps://www.instagram.com/reel/ABC_123/?igsh=YWJj&utm_source=ig_web_copy_link\nCompartido por Ana',
             'https://www.instagram.com/reel/ABC_123/?igsh=YWJj&utm_source=ig_web_copy_link'),
            ('Shared\r\nhttps://instagram.com/p/ABC/?igsh=abc\r\nCaption', 'https://instagram.com/p/ABC/?igsh=abc'),
            ('Apple Support\nhttps://support.apple.com/guide/mac-help/share?locale=es_CL#menu',
             'https://support.apple.com/guide/mac-help/share?locale=es_CL#menu'),
            ('https://example.com/first\nhttps://example.com/second', 'https://example.com/first'),
            ('Read (https://example.com/wiki/Topic_(detail)). Next',
             'https://example.com/wiki/Topic_(detail)'),
            ('Visit “https://example.com/path?q=a%2Cb&x=1”, please.',
             'https://example.com/path?q=a%2Cb&x=1'),
            ('<https://example.com/path> and more', 'https://example.com/path'),
            ('bad https://[broken then https://example.com/good', 'https://example.com/good'),
            ('bad https://example.com:99999 then https://example.com/good', 'https://example.com/good'),
            ('  texto ñ\nsegunda línea  ', 'texto ñ\nsegunda línea'),
            ('ftp://example.com/a and https:///missing-host',
             'ftp://example.com/a and https:///missing-host'),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_content(raw), expected)
                _, digest, body = validate({'id':'share','content':raw})
                self.assertEqual(digest, content_digest(expected, []))
                self.assertEqual(body, validate({'id':'share','content':expected})[2])

    def test_normalized_capture_deduplication_and_format(self):
        url = 'https://www.instagram.com/p/ABC/?igsh=abc&x=1'
        data = {'id':'share','content':'Instagram\n' + url + '\nMira esto', 'tags':['social','leer']}
        self.assertFalse(self.store.capture(data)['duplicate'])
        before = self.inbox.read_bytes()
        for text in (url, 'Otro título: ' + url):
            self.assertTrue(self.store.capture(dict(data, content=text))['duplicate'])
        self.assertEqual(before, self.inbox.read_bytes())
        self.assertEqual(self.store.db.execute("SELECT content FROM captures WHERE id='share'").fetchone(), (url,))
        self.assertIn('#social\n\n' + url + '\n', before.decode())
        self.assertNotIn('<!-- inbox:', before.decode())
        with self.assertRaises(FileExistsError):
            self.store.capture(dict(data, content='https://example.com/different'))
        self.assertFalse(self.store.capture(dict(data, id='different-id'))['duplicate'])

    def test_extracted_url_survives_journal_recovery(self):
        url = 'https://instagram.com/reel/ABC/?igsh=abc'
        data = {'id':'recover-share', 'content':'Watch this\n' + url}
        with patch('server.os.replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.store.capture(data)
        self.assertTrue(self.store.capture(dict(data, content=url))['duplicate'])
        self.assertEqual(self.inbox.read_text().count(url), 1)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM rewrite').fetchone())

    def test_old_share_text_retry_preserves_original_digest_and_content(self):
        text = 'Historical title\nhttps://example.com/old\nHistorical note'
        self.inbox.write_text('<!-- inbox:old-share -->\n#social\n' + text + '\n')
        self.store.migrate()
        before = self.inbox.read_bytes()
        digest = self.store.db.execute("SELECT digest FROM entries WHERE id='old-share'").fetchone()[0]
        self.assertTrue(self.store.capture({'id':'old-share','content':text,'tags':['social','social']})['duplicate'])
        self.assertEqual(before, self.inbox.read_bytes())
        self.assertEqual(self.store.db.execute("SELECT digest FROM entries WHERE id='old-share'").fetchone()[0], digest)
        self.assertEqual(self.store.db.execute("SELECT content FROM captures WHERE id='old-share'").fetchone()[0], text)

    def test_http(self):
        # Exercise the actual handler with in-memory HTTP streams; no listener.
        class Connection:
            def __init__(self, raw):
                self.input = io.BytesIO(raw)
                self.output = io.BytesIO()
            def makefile(self, *args, **kwargs):
                return self.input
            def sendall(self, data):
                self.output.write(data)
            def settimeout(self, timeout):
                pass

        class Server:
            token = 'a' * 40
            store = self.store

        def request(method, path, body='', token=Server.token, content_type='application/json'):
            body = body.encode()
            raw = (f'{method} {path} HTTP/1.0\r\n'
                   f'Authorization: Bearer {token}\r\n'
                   f'Content-Type: {content_type}\r\n'
                   f'Content-Length: {len(body)}\r\n\r\n').encode() + body
            connection = Connection(raw)
            Handler(connection, ('127.0.0.1', 0), Server())
            headers, payload = connection.output.getvalue().split(b'\r\n\r\n', 1)
            return int(headers.split()[1]), json.loads(payload)

        before = self.inbox.read_bytes()
        self.assertEqual(request('GET', '/health', token='wrong'), (200, {'ok': True}))
        self.assertEqual(self.inbox.read_bytes(), before)
        self.assertEqual(request('GET', '/no')[0], 404)
        def post(body, **kwargs):
            return request('POST', kwargs.pop('path', '/capture'), body, **kwargs)
        body = json.dumps({'id': 'http', 'content': 'text'})
        self.assertEqual(post(body, token='wrong')[0], 401)
        self.assertEqual(post(body, path='/no')[0], 404)
        self.assertEqual(post(body, content_type='text/plain')[0], 415)
        self.assertEqual(post('x' * 32769)[0], 413)
        self.assertEqual(post('{')[0], 400)
        shared = {'id':'http-share','content':'Instagram\nhttps://instagram.com/reel/ABC/?igsh=abc'}
        self.assertEqual(post(json.dumps(shared))[0], 200)
        self.assertTrue(post(json.dumps(dict(shared, content='https://instagram.com/reel/ABC/?igsh=abc')))[1]['duplicate'])
        self.assertEqual(post(body)[0], 200)
        self.assertTrue(post(body)[1]['duplicate'])
        self.assertEqual(post(json.dumps({'id': 'http', 'content': 'changed'}))[0], 409)


if __name__ == '__main__':
    unittest.main()
