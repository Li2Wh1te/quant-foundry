# S2-D08 public seams

D08 owns `backend/app/market_service/{service,bindings,contracts,errors,router}.py`,
`frontend/src/features/market-data/data/`, and only the market registration in
`backend/app/main.py`. D09 owns catalog/list/calendar domain modules and pages;
D10 owns series/detail and the plotting boundary; D11 owns factors/adjustments.
No CurrentStore, S3, R01 or collection module was changed. This work adds no
supplier client, collection fallback, execution admission, snapshot or token table.

Only `GET /api/admin/market/capabilities` and
`GET /api/admin/market/instruments/resolve?symbol=...&asset_type=etf` are installed.
Both use the existing bearer authentication and default application factory.
Unsupported stock/index resolution is a scoped capability error. Unimplemented
domain endpoints are not registered as successful stubs.

## Backend composition

Domain routers use `Depends(get_market_service)` from `market_service.router`.
This constructs the sole `create_market_service(settings, engine, owner_scope,
cancelled)` factory with the real DB dependency. Do not inject FormalProvider,
AcceptedRunPlan, account/fee metadata, worker state or a successful resolver.

`service.bindings` is a code-owned registry keyed by:

| Business key | Formal entry | Declared semantics |
|---|---|---|
| `etf-current` | E67 | admitted InstrumentReference fields; source code is not proof of cross-code history |
| `exchange-calendar-local` | E68 | exchange_scope/calendar_date/is_open/scope_basis; previous/next dates are not present in this model |
| `etf-daily-local` | E70 | provider_reported daily OHLC, 元/手/千元; share conversion is not required for display |
| `etf-factors-local` | E69 | tushare_fund_adj factor text; anchor/coverage strategy is not established |

Mappings resolve Arrow columns from `Entry.layout.columns[model, field_name]`,
not field position or a guessed `f0_*` name. `binding.representation` reproduces
the existing LocalInput source/dataset/default-variant identity. Other variants
and sources cannot silently replace it. `binding.partition(subject, object_key)`
uses the actual formal partitioner; `binding.declared_units` exposes only known units.

E51 is not registered as raw. The E70 model explicitly fixes `price_basis` to
`provider_reported`: D08 advertises readable daily observations separately from
unknown raw interpretation. D10 must resolve that interpretation with evidence;
it must not switch suppliers or relabel E51. E69 absence does not close E70 reads.
E67 does not contain management fees, list_status, index_name or manager_name:
those fields stay null without an independently admitted fact.

Use the bounded context:

```python
with service.read_scope(
    (ETF_DAILY, EXCHANGE_CALENDAR),
    conditions={"instrument_key": key, "series_key": ETF_DAILY,
                "period": "day", "adjustment": "none",
                "start": start, "end": fixed_end, "as_of": fixed_as_of},
    identity=IdentityQuery(instrument_keys=(key,), start=start, end=fixed_end),
    cursor=cursor,
) as scope:
    segments = scope.mapping_segments(key)
    page = scope.read(ETF_DAILY, declared_query)
    objects = scope.objects(ETF_DAILY, page)
    # Build the actual domain DTO, then scope.response(dto) before leaving.
```

`declared_query` is the existing CurrentStore Query with `require_qualified=True`
and an exact `representation = binding.representation` filter. Complete objects
must be read before decoding. D09 can bind up to 200 `source_codes` in
`IdentityQuery` for a metadata page; D10 can bind existing `instrument_keys` and
an inclusive effective date window. Corrections fold before symbol/date filters,
and `mapping_segments` reuses the existing evidenced coverage/overlap validator.
The knowledge boundary is always current read_at, not simulated publication time.

`scope.meta()` defaults coverage to unknown/missing_dates=null and business date
to null. Domains provide actual scope, dates and proven coverage separately.
`scope.make_cursor(position)` is signed and stateless; a resumed position is
`scope.position`. Conditions must include all filters and the fixed window/basis.
No cursor reads old content. Tokens bind auth owner, identities, code mapping,
selected bindings, generation/schema/rule, request admission, object-quality
mutations (including xmin changes without generation), coverage proof/pending
inputs, and overflow restrictions.
All reads and DTO construction finish before the short final locked recheck.

Per-store row/file/scan limits continue to apply. The shared session additionally
bounds cumulative input to 100,000 rows / min(4 MiB, store query_bytes) and output
to min(4 MiB, store query_bytes). A larger history must return a budget error or
be implemented as an explicitly budgeted domain stream, never bypass these limits.
The complete session has a 30-second deadline; SQL checkpoints use the smaller
remaining time or the existing 5-second metadata budget.

## Frontend composition

Import `marketClient`, DTOs, safe errors and `createMarketReadSession` from
`features/market-data/data`. The client validates the shared domain contract and
offers typed methods for D09-D11 routes; those later routes still need real domain
implementation. Capability adds explicit `mapping_implemented` and
`metadata_known` booleans beside `request_checked` so support and admission are
separate. Capability probes do not promise an arbitrary date/instrument request.

Pass AbortSignal on each read; dispose the page-owned session on unmount. Use a
condition key including catalog/filter/identity/series/period/basis/fixed window.
A changed condition clears old results. Only network/timeout failures can retain
same-condition stale data, with `canContinue=false`; auth, quality, schema,
maintenance, changed-data or unknown failures clear it. The session races late
responses and listens to the existing logout/auth/permission invalidation bus.

Decimal fields are checked text, not JSON numbers. `exactMarketValue` preserves
tooltips/tables/copy values; only D10's checked plotting adapter may approximate
them. Cursors/results stay in bounded page memory; no localStorage or URL state.

This infrastructure-only package creates no new UI view or screenshots. Existing
market pages remain for D09-D12 migration; no legacy path has yet been deleted.
