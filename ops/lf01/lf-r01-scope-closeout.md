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
