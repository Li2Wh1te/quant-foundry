# R01 current-scope continuation

This repair retains the source/current boundary and does not reset or rebuild
existing outputs. Native failures remain in their active source ranges with a
blocked marker. Successful sibling ranges continue; a real succeeded collection
state makes a failed range eligible again. Up to 32 captured observations of one
native scope share a reduction/commit, with the cursor advancing only afterwards.
Old sealed single-observation work drains with its original source identity.

Current issue sets retain every original issue key, token, exact target,
resolution, attempts and first/last timestamps. Metadata compaction compares
expanded count and evidence digest in the same transaction. No problem becomes
resolved through compaction. Object counts, descriptors and audit counts use
expanded membership; physical catalog records retain their existing finite cap.
Each new exact-key proof can remove only its matching members. Opaque restrictions
remain conservatively global. Query lookups select their real partition ranges
and indexed global restrictions instead of walking unrelated issue rows.

`compact-issues --entry E...` is a metadata-only operation on existing business
entries. It never changes current files, source checkpoints, source configuration,
quality state or store capacity. `verify-coverage --entry E...` reads the complete
native source and preserved v2 data, checking keys, content fingerprints, ordering,
file digests and exact failed-input disposition under unchanged generation.
It saves a coverage receipt only on an actual match; missing/changed/extra objects
and incomplete reads stay pending. It does not alter complete/qualified flags.
For preserved Tushare current snapshots, an additive capture revision fences each
range against concurrent source writes. Only inactive ranges whose revisions
were captured before the verified native read and remain unchanged are
acknowledged, in the same transaction as the coverage receipt. Late commits and
active continuations remain queued. Shared full-coverage/read/audit gates still
reject unresolved issues and queues.

A transactional, trigger-maintained inventory keeps exact physical and member
counts in one row per dataset; the physical capacity remains unchanged. The additive migration counts existing issues
under a writer-excluding lock; failed transactions roll back inventory deltas.
Logical issue counts and independent audit still expand the actual records.
Compaction leaves identical physical records untouched and splits large exact
member groups at the existing per-record JSON boundary. Partition pagination
uses a compound scope/key index.

Verification's fingerprint SQLite file is disposable, never resumed as a sealed
proof. It uses bounded batched commits without syncing each individual source
point, and native scalar Tushare rows use bounded cursor pages. Range acknowledgements
are batched with the same exact revision and inactive-work predicates. All original
file/schema/content checks and the atomic final coverage receipt remain required.

Validation uses synthetic originals, real isolated PostgreSQL and supported local
filesystems. Production operational evidence stays outside commits.

## Small synthetic count and continuation regression

`tests/data_store_r01_fixture.py` supplies one fictional E50 `*` scope with two
overlapping observations and two keys in January/February. An import receipt
without exact returned-key proof makes all four normalized unit occurrences
fail with `SOURCE_CONFIRMATION_UNPROVEN`; the merger retains two exact problems.
Collection state `succeeded` still does not prove those keys.

The native-source test checks that a state-only producer update retains pending
work even with no newer observation. The current-store test cancels after the
first partition: its catalog checkpoint and SQLite cursor survive, while the
native observation cursor remains unchanged until the whole batch commits.
Resume must decode zero source payloads, commit only the second partition, leave
the first checkpoint/files unchanged, and retain a range dirtied before final
acknowledgement. Draining that state-only event writes no partition; both problem
keys still fail qualified reads.

For this fixture, the cancelled call reports four failed unit occurrences, one
partition commit and two partition loops. Resume reports zero newly normalized
units, one partition commit and two loops (including the final empty loop).
There are two retained issues and two generation advances in total. Neither
loop counts nor generation advances count distinct confirmed business keys.

With the normal returned-key receipt, January receives its exact acquisition
order/token and February keeps its inherited basis. With the receipt missing,
`unconfirmed_keys` overrides retained `row_basis`: invalid markers use the new
observation order/token so old valid output cannot hide the missing proof.

Reproduce with isolated PostgreSQL enabled, the repository's test environment
and a supported ext4/XFS/Btrfs test directory:

```sh
cd backend
uv run --locked python -m pytest -q \
  tests/test_data_store_local_adapters.py \
  tests/test_data_store_incremental.py -k r01
```

An unsupported cloud filesystem must report `UNSUPPORTED_FILESYSTEM`; do not
inject the optional overlay probe to certify these commits. The existing
Validate workflow runs these tests in its full backend suite on the supported
runner filesystem; the separate current-store acceptance job checks its kernel
subset and cross-container locks on a supported volume.

The most useful additive diagnostics would report failed unit occurrences,
retained logical problem keys, successful partition commits and partition loops
as separate measures; also expose sealed partitions remaining, the native
batch cursor, and whether final acknowledgement retained a producer's pending
bit. Read existing summaries/checkpoints/queues for this; do not introduce a
per-key success ledger or infer range completion from a net queue-count delta.
These observations validate local semantics, not production coverage or audit
acceptance.
