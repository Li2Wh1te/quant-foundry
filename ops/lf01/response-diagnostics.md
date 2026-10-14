# Bounded response diagnostics for R01

The two supported datasets are fund_income and fund_performance_history.
Ordinary runs gain correlated request/outcome metadata. Original business request
parameters and original UTF-8 response text are file-only and disabled by default.
Symbols and business values are retained. Passwords, API keys, tokens, cookies,
authorization values and credential-bearing URL userinfo are removed.

The existing app.jsonl handler is reused. A per-handler copy removes originals
from stderr, and the diagnostic renderer ignores ambient context fields. No
database, new log store, credentials, supplier endpoint, retry or checkpoint
policy is added. Existing business errors stay errors.

## Explicit run window

The operator may set QF_THS_DIAGNOSTIC_RUN_IDS to one or two exact NEW run UUIDs
and QF_THS_DIAGNOSTIC_UNTIL to an aware ISO timestamp at most 30 minutes ahead.
Both are required. Only the two supported datasets can capture originals.
Do not reuse the failed run ID. Keep the parent/new-run mapping in the existing
controlled operation receipt.

Capture is bounded to 120 existing HTTP attempts, 8 MiB per complete response and
16 MiB of encoded original fields per run. These are capture ceilings, not
permission to issue requests. Original task/object/date/HTTP/fee budgets still
apply. Capture expiry, incomplete responses, body/run size limits, non-JSON text
and credential-filter/encoding failures have explicit states. Never call a
truncated prefix a complete original. Unknown status/code/coverage flags stay
null; has_more=false is not a fabricated coverage guarantee.

Non-200 bodies are read only for an explicitly enabled run, from the SAME
response, within the existing deadline/byte ceiling. A diagnostic read failure
does not replace the original HTTP error or add a request. UTF-8 non-JSON text
can be retained after credential filtering; undecodable content is explicitly
omitted. If credentials are absent, JSON whitespace and numeric text remain
unchanged. If credentials require rewriting, Decimal business values remain JSON
numbers at their original precision.

## R01-only build, after merge

Main contains unrelated runtime work. Do not build or deploy the whole checkout.
Use the exact reviewed merged commit and scripts/r01_diagnostic_overlay.py.
It reads five hash-pinned R01 source files from an exact, already approved local
base image (a never-started container) and adds only the diagnostic hooks/module.
It never edits the running container. The Docker build has no network, installs
nothing and changes no dependencies, migrations or lifecycle/stop protocol.

On a retained local source root, --source-root allows an offline preparation
check. Otherwise the script extracts those five inputs from --base-image.
--output must be a new owned build directory. --build-tag additionally builds the
overlay; it does not deploy. Preserve overlay-manifest.json and the base/result
image IDs. The caller must first verify the base image is the approved R01
runtime, not merely an image with similar filenames.

## Gates before any production retry

1. Recover the exact original selected objects and effective date range, including
   inherited source/variant/revision effects. Omitted automatic dates are not
   permission to use a fresh expanded range.
2. Determine the original logical/HTTP request ceiling and applicable fee/quota.
   Unknown scope, request cost or fee is a blocker; do not guess.
3. Prove the controlled run cannot overlap normal scheduled work for that scope.
   Pausing a plan alone does not fence already queued work. Use the existing
   admitted maintenance/drain protocol when a deployment needs a stop.
4. Only after those gates, create a new associated run and enable its exact
   capture window. Preserve the failed run, source quality guards and failed
   checkpoint. Do not fill zero, mark empty as success or replay outside scope.
5. Inspect the original log on the execution host. Keep original business
   parameters/body there; export only necessary findings, hashes and metadata.
   Remove capture enablement after the bounded window. No original log belongs
   in GitHub, a source bundle, the Mac workspace or a chat message.

No past response can be reconstructed by this patch. A future retry can establish
only what that new request actually returned.
