"""Reproducible, offline SQLite history microbenchmark; no LLM or live data.

Run from any directory: python benchmarks/history_read.py --turns 100000 --repeats 7
The temporary database is deleted after the run. JSON is printed and optionally
saved with --output. Both methods use the SAME indexed database and payloads.
"""
import argparse
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from customer_profile_agent.profile_store import Store  # noqa: E402

BASELINE_SQL = ('SELECT mid,message,result FROM turns WHERE cid=? '
                'AND status="done" ORDER BY created')
LIMITED_SQL = ("SELECT mid,message,result FROM turns WHERE cid=? AND status='done' "
               'ORDER BY created DESC,mid DESC LIMIT ?')


def old_read(store):
    """Original full fetch/decode/materialize, then the agent's last-40 slice."""
    with store.connect() as db:
        rows = db.execute(BASELINE_SQL, ('synthetic-customer',)).fetchall()
    out = []
    for mid, message, result in rows:
        out.extend([{'role': 'user', 'content': message, 'message_id': mid},
                    {'role': 'assistant', 'content': json.loads(result)['answer']}])
    return out[-40:]


def seed(store, turns):
    def rows(cid, count, status):
        for n in range(count):
            yield (cid, f'm{n:09}', f'Fictional request {n:09}: ' + 'x' * 128,
                   '[]', status,
                   json.dumps({'answer': f'Fictional answer {n:09}: ' + 'y' * 128}),
                   1700000000.0 + n)

    with store.connect() as db:
        db.executemany('INSERT INTO turns VALUES(?,?,?,?,?,?,?)',
                       rows('synthetic-customer', turns, 'done'))
        db.executemany('INSERT INTO turns VALUES(?,?,?,?,?,?,?)',
                       rows('unrelated-customer', turns // 10, 'done'))
        # Distinct IDs retain the same customer but must not enter its history.
        db.executemany('INSERT INTO turns VALUES(?,?,?,?,?,?,?)',
                       ((cid, 'failed-' + mid, msg, hist, status, result, created)
                        for cid, mid, msg, hist, status, result, created in
                        rows('synthetic-customer', turns // 50, 'failed')))


def command_output(*command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def summary(samples):
    return {'samples_ms': samples, 'median_ms': statistics.median(samples),
            'min_ms': min(samples), 'max_ms': max(samples)}


def benchmark(turns, repeats):
    with tempfile.TemporaryDirectory(prefix='profile-history-benchmark-') as directory:
        store = Store(Path(directory) / 'synthetic.sqlite3')
        seed(store, turns)
        read_methods = {'full_then_slice': lambda: old_read(store),
                        'limited': lambda: store.history('synthetic-customer', limit=40)}
        expected = read_methods['full_then_slice']()
        if expected != read_methods['limited']():
            raise AssertionError('history results differ')
        samples = {name: [] for name in read_methods}
        # One unmeasured correctness/warmup read per method above; alternate
        # method order each repetition. No OS-cache eviction is attempted.
        for iteration in range(repeats):
            names = list(read_methods)
            if iteration % 2:
                names.reverse()
            for name in names:
                start = time.perf_counter_ns()
                result = read_methods[name]()
                duration = (time.perf_counter_ns() - start) / 1_000_000
                if result != expected:
                    raise AssertionError(f'{name} changed its result')
                samples[name].append(round(duration, 6))
        with store.connect() as db:
            plans = {
                'full_then_slice': db.execute('EXPLAIN QUERY PLAN ' + BASELINE_SQL,
                                              ('synthetic-customer',)).fetchall(),
                'limited': db.execute('EXPLAIN QUERY PLAN ' + LIMITED_SQL,
                                     ('synthetic-customer', 20)).fetchall(),
            }
            pragmas = {key: db.execute('PRAGMA ' + key).fetchone()[0]
                       for key in ('journal_mode', 'page_size', 'cache_size')}
        measured = {name: summary(values) for name, values in samples.items()}
        code_paths = ('src/customer_profile_agent/profile_store.py',
                      'src/customer_profile_agent/file_store.py',
                      'src/customer_profile_agent/agent.py', 'benchmarks/history_read.py',
                      'tests/test_history.py')
        return {
            'schema_version': 1,
            'measured_at_utc': datetime.now(timezone.utc).isoformat(),
            'scope': 'Synthetic local SQLite history-read microbenchmark; not production, HTTP throughput, LLM latency, or concurrent-load evidence.',
            'environment': {'python': sys.version, 'sqlite': sqlite3.sqlite_version,
                            'platform': platform.platform(), 'machine': platform.machine(),
                            'logical_cpus': os.cpu_count(),
                            'cpu': command_output('sysctl', '-n', 'machdep.cpu.brand_string') or platform.processor(),
                            'memory_bytes': command_output('sysctl', '-n', 'hw.memsize'),
                            'sqlite_pragmas': pragmas},
            'dataset': {'target_completed_turns': turns,
                        'other_customer_completed_turns': turns // 10,
                        'target_failed_turns': turns // 50,
                        'total_rows': turns + turns // 10 + turns // 50,
                        'created': 'unique 1700000000 + turn number within each group',
                        'message': 'Fictional request + zero-padded turn number + 128 x characters',
                        'answer': 'Fictional answer + zero-padded turn number + 128 y characters',
                        'database_bytes': store.path.stat().st_size,
                        'temporary_database_deleted_after_run': True},
            'method': {'repeats_per_method': repeats, 'warmup_per_method': 1,
                       'order': 'alternating by repetition', 'cache': 'warm; no OS-cache eviction',
                       'timing': 'new SQLite connection, SQL fetch, JSON decode, dict construction, slice and intermediate cleanup',
                       'same_database_and_index': True, 'requested_messages': 40,
                       'full_turns_fetched': turns, 'limited_turns_fetched': min(turns, 20)},
            'results_equal': True, 'returned_messages': len(expected),
            'result_sha256': hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
            'measurements': measured,
            'median_ratio_full_over_limited': measured['full_then_slice']['median_ms'] / measured['limited']['median_ms'],
            'explain_query_plan': plans,
            'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in code_paths},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--turns', type=int, default=100000)
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.turns < 20 or args.repeats < 2:
        parser.error('--turns must be >=20 and --repeats must be >=2')
    rendered = json.dumps(benchmark(args.turns, args.repeats), ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
