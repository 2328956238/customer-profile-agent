# Bounded conversation history: implementation and local evidence

Measured on **2026-10-07**. This is a synthetic, offline SQLite history-read
microbenchmark. It is not evidence of production API latency, concurrent
throughput, model response speed, or business impact. No LLM was called and no
customer data was used.

## Problem and change

The agent previously called `store.history(cid)` and then retained the last 40
messages in Python. SQLite fetched every completed turn for that customer and
decoded every result JSON before discarding older messages. Work increased with
the lifetime history even though the prompt used only 40 messages.

`history(cid, limit=None)` now accepts an optional **message** count:

- Omitting the limit still returns all completed messages. A zero limit returns
  an empty list without storage I/O; negative values and non-integers (including
  booleans) raise `ValueError`.
- SQLite uses a partial index on `(cid, created, mid) WHERE status='done'`, reads
  at most `ceil(limit / 2)` completed turns in descending order, then restores
  chronological order and applies the exact message boundary. An odd limit can
  begin with an assistant message, matching the previous `full_history[-limit:]`
  behavior. Equal timestamps are now explicitly ordered by message ID.
- The agent requests `limit=40` when it uses stored history. Caller-supplied
  nonempty history still takes precedence and is limited to its last 40 entries;
  an empty list still falls back to storage, as before.
- FileStore implements the same last-N-message contract relative to its existing
  output. Unlike SQLite, it previously omitted empty assistant answers; this
  behavior is retained. It counts actual messages while walking newest turns,
  so many empty answers do not accidentally return too few messages.

The FileStore change **does not** eliminate reading and sorting the entire JSON
file. The SQLite change does not make database operations asynchronous, provide
cross-process conversation locks, or prove multi-instance safety. Creating the
new index on an existing large database has a one-time cost that is not measured
below; future writes also maintain the index.

## Reproduce

From the repository root, using Python 3.11+ (standard library only):

```bash
python benchmarks/history_read.py --turns 100000 --repeats 7 --output /tmp/history-upgrade.json
PYTHONPATH=src python -m unittest discover -s tests -p 'test_history.py' -v
PYTHONPATH=src python -m unittest discover -s tests -p 'test_core.py' -v
```

The recorded run used bundled Python 3.12.14. The script creates a local temporary
database, checks equality, measures both methods, emits JSON, and deletes the
database. The JSON records raw timing samples, environment, query plans, dataset
shape, output digest and source SHA-256 hashes. It does not install dependencies.

Both methods run against the **same database, with the new index already
present**. The baseline reproduces the original full-fetch/decode/build/slice
algorithm; it is not a separate pre-index database benchmark. This isolates the
benefit of bounding the read and materialization rather than combining it with
an unindexed comparison. The synthetic timestamps are unique within each data
group; equal-timestamp correctness is covered separately by the unit tests.

## Recorded conditions

| Item | Value |
| --- | --- |
| Machine | Apple M4 Max, arm64, 16 logical CPUs, 48 GiB RAM |
| OS | macOS 26.5.1 |
| Runtime | Python 3.12.14; SQLite 3.53.1 |
| SQLite settings | journal mode `delete`; page size 4096; cache size -2000 |
| Dataset | 100,000 completed turns for the target customer, 10,000 completed turns for another customer, 2,000 failed target turns |
| Payload | Fixed synthetic request/answer prefixes, zero-padded turn number and 128 repeated ASCII characters per message |
| Database size | 56,225,792 bytes including tables and indexes |
| Request | Last 40 messages; new query reads 20 turns instead of 100,000 |
| Sampling | One unmeasured warmup/equality read per method, then 7 timed reads each, alternating method order |
| Cache conditions | Warm local reads; no OS-cache eviction or cold-start claim |
| Timed work | Connection, query/fetch, JSON decoding, message dictionaries, slicing and intermediate cleanup |
| Excluded | Database generation, index creation, HTTP, network, LLM calls, writes and concurrent clients |

## Results

| Method | Median | Minimum–maximum | Messages returned |
| --- | ---: | ---: | ---: |
| Original full history then last-40 slice | 113.758 ms | 111.244–117.856 ms | 40 |
| Indexed limited history | 0.243 ms | 0.107–0.265 ms | 40 |

The returned lists were equal on the warmup and every measured read. Their
canonical JSON SHA-256 was
`c19d9886ecdc5564dbee24fa9fc8e07eec96993ede9f2d1d18102ee9343cbc52`.
Absolute timings will differ on other machines, SQLite versions, cache states,
payload sizes and history lengths. Sub-millisecond results are especially
sensitive to local scheduling and cache effects; retain the range with the median.

Both `EXPLAIN QUERY PLAN` outputs reported:

```text
SEARCH turns USING INDEX turns_done_history (cid=?)
```

Neither needed a temporary sort. The limited query used
`ORDER BY created DESC, mid DESC LIMIT 20`; the baseline fetched all 100,000
completed target turns. Sharing an index does not mean the methods perform the
same amount of work.

## Correctness evidence

The new `tests/test_history.py` contains **11 tests**, all passing locally:
message limits (including odd counts and limits beyond available history), zero,
negative/invalid values, completed-status filtering, customer isolation, equal
timestamps and restart, empty-answer behavior, long runs of empty answers,
bounded JSON decoding and query-plan checks, existing-database index creation,
and both stored/external agent history paths. The existing **24 core tests** also
passed. API/authorization integration is a separate test surface; these counts
do not claim to cover it.

## Code snapshot

The measured JSON records these SHA-256 hashes, allowing later changes to be
distinguished from this run:

| File | SHA-256 |
| --- | --- |
| `src/customer_profile_agent/profile_store.py` | `1c688a236504651f6c46e0aab02946128aa02bfdba3aa7bd3b07a38aec3024ed` |
| `src/customer_profile_agent/file_store.py` | `f41d2a6d994f3a23949091cdad9198b7ba6e1029984525863229e3835fa3677f` |
| `src/customer_profile_agent/agent.py` | `5af773859856516c5f7b941cc92550d527779ce8bb9aea56f40c197cb50704cf` |
| `benchmarks/history_read.py` | `004159a08a24b39171a1f310dae5c95313a3e05f78733a703835397d09f48994` |
| `tests/test_history.py` | `baa2465ea6d940c242917409d4509be3fcb4a08a1d4d5fcfc218f69d4bad615c` |

For an interview, describe the bounded-read design, compatibility edge cases and
measurement conditions first. Do not relabel this result as an online production
speedup or an improvement to the entire AI response pipeline.
