# D11 bounded results, qf.results.v1

`ResultRecord` admits business results only: summary, equity, order transitions,
fills, final positions, company actions, explicit user records and bounded logs.
It has no MarketEvent/market snapshot or per-Tick success variant.
D10's indexed trade-page adapter delegates to `FileResultSink`; it no longer
maintains a second file format or an all-history list.

The producer obtains a `SinkCredit` before a contiguous, nonempty bounded batch.
Default writer credit is <=1024 records and <=1 MiB. The file writer is synchronous:
completion provides backpressure without a queue. `CreditGate` is a one-slot
interruptible credit handshake for D12 IPC; cancel, close and deadlines interrupt
waits. D12 must connect its actual channel lifecycle to this gate.

Defaults: total 256 MiB (trusted worker may set <=8 GiB), shards <=4 MiB, <=1024
shards, <=1 MiB control/page response, <=10,000 rows/page, <=1 MiB/10,000 log
messages, <=100,000 record points, <=128 record names and <=64 fields/point.
There are at most eight open kind partitions, not one descriptor per historical
shard. Serialization, reads and checksum buffers are bounded. A record that
cannot fit one legal page fails instead of creating an unreadable result.

Each server-generated `<kind>-NNNNNN.data` contains repeated little-endian u32
JSON byte length + JSON business record. Its `.index` contains u64 little-endian
data offset and u64 global result sequence per item. Sealed descriptors contain
kind, per-kind start/count, global first/last sequence, exact file byte counts,
and SHA256 for data and index. Gaps in global sequence can represent visibly
dropped optional logs; required business rows never disappear this way.
The small `manifest.json` contains schema, run/outcome/partial, descriptors,
counts, byte accounting, output notices, execution-model assumptions and the summary. JSON money/time/sequence
are strings; statistical floats are explicitly finite.

Required output exceeding capacity, order/fill write failure, digest mismatch,
missing final summary, protocol discontinuity and cancellation cannot finalize
success. All active files are synchronized and sealed, earlier sealed files are
rechecked, the manifest is written/synchronized and renamed, and the directory
is synchronized before the writer exposes succeeded. On failure/cancel, abort
marks partial and invalidates complete return statistics. Partial diagnostics
may fail to persist on a full/failed device; the primary error remains failed,
and D12/D13 must retain their authoritative small run state independently.

Parent roots are trusted configuration, never SDK/strategy input. Every path
component is opened without following symlinks; further access is relative to
held directory descriptors. Names are generated, files use exclusive creation,
0700 directories/0600 files, and nonregular/multiply-linked files are refused.
Root replacement refuses finalization; owner-drop cleanup only unlinks its own
names through the held descriptor and never removes a replacement directory.
This is tested file handling, not a Python process/security sandbox.

`retain()` transfers a committed result directory's lifetime to the trusted
supervisor/service. Temporary worker handles otherwise remove only their own
files on close/drop. `FileResultReader::open` reads one <=1 MiB manifest, checks
run/schema/names/counts/size invariants, and uses the same bounded indexed pages
with shard digest verification. Active/retained cursors bind kind and run/owner
instance. D13 still owns authentication, owner isolation, durable storage policy,
DB/API integration and claim/fence arbitration; a checksum is not authorization.

D12/D13 must coordinate the final small success commit with the accepted gateway
consistency guard and claim/cancel winner. `finalize_guarded` prepares all
files before acquiring a caller-owned guard and holds it across manifest commit.
The current wheel rechecks and locks its D04 channel at that boundary; that check is not a production transaction
across service/data state. No deployment or full process isolation is claimed.

Installed `RunResults.summary()` and `.page(kind=..., limit=100, cursor=...)`
return immutable bounded data with Decimal money and integer ns. There is no
implicit all-pages iterator or stored page cache. SDKs do not receive shard
paths, DSNs, vendor keys or mounts.
The terminal portfolio contains scalar balances and a position count/page kind;
it never embeds all held positions in the control summary.
D14's HTTP transport remains a separate task; `BacktestClient.result`
validates the same kinds/limits but cannot submit/read
an unimplemented service.
