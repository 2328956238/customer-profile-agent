"""Atomic file persistence for the local demo (Linux/macOS only)."""
import copy
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from .profile_store import blank, Conflict


class FileStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def _path(self, cid):
        return self.path / (hashlib.sha256(cid.encode()).hexdigest() + '.json')

    @contextmanager
    def transaction(self, cid):
        path = self._path(cid)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                body = json.loads(path.read_text()) if path.exists() else {'cid': cid, 'profile': blank(), 'turns': {}}
                yield body
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write(self, cid, body):
        path = self._path(cid)
        tmp = path.with_suffix('.tmp')
        with tmp.open('w') as f:
            os.chmod(str(tmp), 0o600)
            json.dump(body, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
        fd = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def load(self, cid):
        with self.transaction(cid) as body:
            return copy.deepcopy(body['profile'])

    def save(self, cid, profile, expected):
        if type(expected) is not int or expected < 0:
            raise ValueError('expected revision must be a nonnegative integer')
        with self.transaction(cid) as body:
            if body['profile']['revision'] != expected:
                raise Conflict('profile revision changed; reload and retry')
            updated = copy.deepcopy(profile)
            updated['revision'] = expected + 1
            body['profile'] = updated
            self._write(cid, body)
            return updated

    def begin_turn(self, cid, mid, message, history):
        with self.transaction(cid) as body:
            old = body['turns'].get(mid)
            if old:
                if old['message'] != message:
                    raise ValueError('message id reused with different content')
                if old['status'] == 'done': return copy.deepcopy(old['result'])
                if old['status'] == 'running':
                    raise Conflict('this message is running; recover explicitly after checking the worker has stopped')
            body['turns'][mid] = dict(message=message, history=history, status='running',
                result=None, created=(old or {}).get('created', time.time()), updated=time.time())
            self._write(cid, body)

    def finish_turn(self, cid, mid, result, status='done'):
        with self.transaction(cid) as body:
            body['turns'][mid].update(status=status, result=result, updated=time.time())
            self._write(cid, body)

    def recover_turn(self, cid, mid):
        """Operator-only recovery; never steal a live worker's message."""
        with self.transaction(cid) as body:
            row = body['turns'].get(mid)
            if not row or row['status'] != 'running':
                return False
            row.update(status='failed', updated=time.time())
            self._write(cid, body)
            return True

    def history(self, cid):
        with self.transaction(cid) as body:
            out = []
            for mid, row in sorted(body['turns'].items(), key=lambda kv: kv[1]['created']):
                if row['status'] != 'done': continue
                out.append(dict(role='user', content=row['message'], message_id=mid))
                if row['result'].get('answer'):
                    out.append(dict(role='assistant', content=row['result']['answer']))
            return out

    def profiles(self):
        for path in self.path.glob('*.json'):
            body = json.loads(path.read_text())
            yield body['cid'], body['profile']
