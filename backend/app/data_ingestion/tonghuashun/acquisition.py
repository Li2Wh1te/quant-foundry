"""Endpoint-aware reads with explicit pagination, date and disclosure semantics."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.data_ingestion.clients.tonghuashun import TonghuashunError

from app.data_ingestion.tonghuashun.contracts import (
    CollectionError, CollectionParameters, Dataset, SHANGHAI, date_ms, items,
    provider_date, validate_bars, validate_ticker, windows, years_before,
)


class DirectorySnapshotChanged(CollectionError):
    """Only a cross-page snapshot change permits restarting a directory read."""


class Acquisition:
    def __init__(self, client, *, repair_native_keys=None):
        self.client = client
        # Only the registered service resolves these raw identities from pinned
        # immutable native input. They are process-local, never a second ledger.
        self.repair_native_keys = repair_native_keys
        self.requests: list[dict] = []
        self.failures: list[dict] = []
        self.fetched_count = 0
        # Keep only the current ETF/index bar window for failure diagnostics.
        # This is not a successful response or a returned-key receipt: request
        # errors occur before read() appends such proof, and cannot confirm data.
        self.bar_request_context: dict | None = None

    def read(self, interface: str, params: dict) -> dict:
        from app.data_ingestion.tonghuashun.control import control
        monitor = control()
        if monitor:
            data, request_id = monitor.read(interface, params, lambda: self.client.request(interface, params))
        else:
            response = self.client.request(interface, params)
            data, request_id = response.data, response.request_id
        from app.data_ingestion.tonghuashun.control import default_repair
        if monitor and default_repair.get() is not None:
            monitor.emit(request_stage='response_validation')
        # Store only allowlisted request parameters and the sanitized trace ID;
        # neither connection URL nor headers can enter source versions.
        # Compact *actual returned-key* evidence. A requested date range alone
        # cannot reconfirm an old point omitted by a sparse response. D02 reads
        # these raw receipts without invoking this client or changing a source
        # toggle. No prices/member payloads or credentials are copied here.
        receipt = {"interface": interface, "parameters": params, "request_id": request_id}
        from app.data_ingestion.tonghuashun.confirmation import returned_keys
        receipt.update(returned_keys(data))
        self.requests.append(receipt)
        records = data.get("item", data.get("abilities", []))
        if isinstance(records, list):
            self.fetched_count += len(records)
        return data

    def report_read(self, interface: str, params: dict) -> dict | None:
        """Missing reports are independent retry units, not proof of no history.

        Preserve successful reports from a long backfill while leaving the
        subject incomplete. Authentication and account throttling still abort
        immediately instead of continuing to issue unrelated requests.
        """
        try:
            return self.read(interface, params)
        except TonghuashunError as exc:
            if exc.kind in ("unauthenticated", "forbidden", "rate_limited"):
                raise
            self.failures.append({"interface": interface, "parameters": params, "error_kind": exc.kind})
            return None

    def directory(self, spec: Dataset, asset: str) -> dict:
        # Restart the entire read; never combine rows from different attempts.
        # Keep retries inside BudgetedClient so account-wide pacing still holds.
        for attempt in range(3):
            try:
                return self._directory_snapshot(spec, asset)
            except DirectorySnapshotChanged:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable directory attempt")

    def _directory_snapshot(self, spec: Dataset, asset: str) -> dict:
        # The documented cap avoids unnecessary snapshot boundaries for most
        # asset types, while retaining pagination for large OTC fund catalogues.
        page_size = 10_000
        all_rows, seen, timestamp = [], set(), None
        for offset in range(0, 1_000_000, page_size):
            data = self.read(spec.interface, {"asset_type": asset, "limit": page_size, "offset": offset})
            rows = items(data, allow_empty=True)
            current = data.get("timestamp")
            if type(current) is not int or current < 0:
                raise CollectionError("标的目录缺少有效的上游快照时间。")
            if timestamp is not None and timestamp != current:
                raise DirectorySnapshotChanged("分页期间上游目录版本发生变化，本次未发布不一致目录。")
            timestamp = current
            if len(rows) > page_size:
                raise CollectionError("目录分页超过请求条数。")
            for row in rows:
                validate_ticker(row, asset)
                if row["thscode"] in seen:
                    raise CollectionError("目录分页出现重复代码，本次未推进完成标记。")
                seen.add(row["thscode"])
                all_rows.append(row)
            if len(rows) < page_size:
                if not all_rows:
                    raise CollectionError("完整标的目录为空，未覆盖已有记录。")
                return {"timestamp": timestamp, "item": sorted(all_rows, key=lambda r: r["thscode"])}
        raise CollectionError("目录分页超过安全上限，本次未推进完成标记。")

    def fetch(self, spec: Dataset, subject: str, parameters: CollectionParameters,
              previous: dict | None, now: datetime) -> dict:
        self.bar_request_context = None
        from app.data_ingestion.tonghuashun.control import default_repair
        repair = default_repair.get()
        if repair is not None:
            return self.repair_default(spec, subject, parameters, previous, now, repair)
        if spec.kind == "directory":
            return self.directory(spec, subject)
        if spec.kind == "bars":
            return self.bars(spec, subject, parameters, previous, now)
        if spec.kind == "nav":
            return self.nav(spec, subject, parameters, previous, now)
        if spec.kind == "financials":
            return self.financials(spec, subject, parameters, previous, now)
        if spec.kind == "indicators":
            return self.indicators(spec, subject, parameters, previous, now)
        if spec.kind == "reports":
            return self.reports(spec, subject, parameters, previous)
        params = {} if spec.kind == "calendar" else {spec.identity: subject}
        if spec.key == "fund_holders":
            params["merge_scope"] = "all"
        if spec.key == "fund_top_holders":
            params["limit"] = 10
        data = self.read(spec.interface, params)
        rows = items(data, allow_empty=spec.allow_empty)
        if not rows and previous and previous.get("item"):
            raise CollectionError("本次空响应与已有非空记录冲突，未覆盖已有成功版本。")
        if spec.kind in ("profile", "company") or spec.key == "fund_manager":
            if len(rows) != 1 or rows[0].get(spec.identity) != subject:
                raise CollectionError("资料响应的标的或关联对象与请求不一致。")
        if spec.identity == "thscodes" and (len(rows) != 1 or rows[0].get("thscode") != subject):
            raise CollectionError("行情或估值快照缺少请求标的，或返回了其他标的。")
        if spec.key == "stock_actions" and data.get("thscode") != subject:
            raise CollectionError("除权除息事件的标的与请求不一致。")
        if spec.kind == "profile":
            for key in ("manager_info", "trade_rule", "rate_info"):
                if key in rows[0] and not isinstance(rows[0][key], list):
                    raise CollectionError("基金资料的嵌套字段不符合接口约定。")
            for key in ("fund_scale", "unit_nav"):
                value = rows[0].get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0):
                    raise CollectionError("基金规模或净值包含非法数值。")
        if spec.kind == "calendar":
            seen = set()
            for row in rows:
                day = provider_date(row.get("date_ms"))
                if row.get("date") != day.strftime("%Y%m%d") or day in seen:
                    raise CollectionError("交易日历存在重复或不一致日期。")
                seen.add(day)
        if spec.key == "index_constituents" or spec.kind == "index_catalog":
            codes = [row.get("thscode") for row in rows]
            if any(not isinstance(code, str) or "." not in code for code in codes) or len(codes) != len(set(codes)):
                raise CollectionError("指数目录或成分快照包含非法、重复代码。")
            data = {**data, "item": sorted(rows, key=lambda r: r["thscode"])}
        # Preserve provider metadata and units verbatim. Current holdings and
        # valuations remain observations, never invented historical facts.
        return data

    def repair_default(self, spec, subject, parameters, previous, now, repair):
        """Read one approved target while preserving its original-default body.

        Selection cannot change ordinary dated variants or broaden into a
        rolling collection. The service has already verified the pinned native
        head (and an original financial period) before reaching this method.
        Only actual successful requests may remove an identical old failure.
        """
        from types import SimpleNamespace
        from app.data_store.adapters.canonical import NativeInputError
        repair.validate()
        if (spec.key, subject) != (repair.dataset, repair.subject) or previous is None:
            raise CollectionError('修复目标与原默认采集范围不一致。')
        if self.repair_native_keys is None:
            raise CollectionError('修复缺少已经核验的原始日期键。')
        if spec.key == 'stock_daily':
            if previous.get('adjust') not in (None, 'none'):
                raise CollectionError('股票单日修复不能混合复权基准。')
            # These dates steer an internal read only. The service's ordinary
            # parameters have no dates and its head remains "default". Bars
            # already validate the exact interval, values and actual identity.
            selected = parameters.model_copy(update={'start_date': repair.day, 'end_date': repair.day})
            fetched = self.bars(spec, subject, selected, previous, now)
            target_key, = self.repair_native_keys
            if (not self.requests or self.requests[-1].get('returned_keys', {}).get('date_ms') != [target_key]):
                # General date validation accepts the same calendar date. An
                # exact repair additionally requires the actual native key;
                # presence of the old target in the merged body proves nothing.
                raise CollectionError('股票修复响应未实际返回确切日期键。')
            if any(envelope.get('adjust') not in (None, 'none')
                   for envelope in fetched['provider_envelopes']):
                raise CollectionError('股票单日修复不能混合复权基准。')
            if any(row.get('thscode', subject) != subject for row in fetched['item']
                   if row['date_ms'] == target_key):
                raise CollectionError('历史行情响应标的与请求不一致。')
            # Preserve the full historical request bounds and unrelated source
            # metadata. A one-day response must not recast inherited rows as a
            # new one-day source window or update their confirmation basis.
            result = {**previous, 'item': fetched['item']}
        elif spec.kind == 'reports':
            result = self.repair_report(spec, subject, previous, repair)
        else:
            data = self.read(spec.interface, {'thscode': subject})
            rows = items(data)
            if data.get('thscode', subject) != subject or any(data.get(name) for name in ('has_more', 'next_cursor', 'next_page')):
                raise CollectionError('财报修复响应主体或完整窗口不一致。')
            # A requested selector is not a supplier filter. All fresh rows
            # must have real periods, and the exact original pair must actually
            # be returned. Never fill a missing period with the plan's dates.
            periods = []
            raw_periods = []
            for row in rows:
                first, last = provider_date(row.get('start_date_ms')), provider_date(row.get('end_date_ms'))
                if first > last or row['start_date_ms'] > row['end_date_ms']:
                    raise CollectionError('财报修复响应缺少完整且一致的实际期间。')
                periods.append((first, last))
                raw_periods.append((row['start_date_ms'], row['end_date_ms']))
            if len(set(periods)) != len(periods) or self.repair_native_keys not in raw_periods:
                raise CollectionError('财报修复响应未命中原始确切期间。')
            from app.data_store.adapters.financial_windows import financial_body
            try:
                financial_body(SimpleNamespace(dataset=spec.key, subject=subject), data)
            except NativeInputError:
                raise CollectionError('财报修复响应完整窗口校验失败。') from None
            # Financial windows are direct source observations, not merged
            # histories. Earlier immutable originals remain in the same store;
            # appending their rows here would invent a fresh direct confirmation.
            result = dict(data)
        old_failures = previous.get('failed_requests', [])
        if not isinstance(old_failures, list):
            raise CollectionError('原默认版本的失败请求结构无法核对。')
        remaining = [failure for failure in old_failures if not any(
            isinstance(failure, dict) and failure.get('interface') == request['interface']
            and failure.get('parameters') == request['parameters'] for request in self.requests)]
        if remaining or 'failed_requests' in previous:
            result['failed_requests'] = remaining
        return result

    def repair_report(self, spec, subject, previous, repair):
        """Require a fresh exact directory entry and a complete fresh group.

        Historical descriptors retained alongside untouched wrappers describe
        materialized old history only. They never become target candidates or
        appear in current_provider_directory as freshly returned descriptors.
        """
        from types import SimpleNamespace
        from app.data_store.adapters.canonical import NativeInputError
        from app.data_store.adapters.portfolio_windows import directory as validate_directory, portfolio_body
        directory_key = spec.interface.replace('-history', '-report-dates')
        current = self.read(directory_key, {'thscode': subject})
        if current.get('thscode', subject) != subject or any(current.get(name) for name in ('has_more', 'next_cursor', 'next_page')):
            raise CollectionError('持仓修复目录主体或完整范围不一致。')
        old_directory = previous.get('report_directory', {'item': []})
        try:
            fresh_descriptors = validate_directory(current)
            old_descriptors = validate_directory(old_directory)
        except NativeInputError:
            raise CollectionError('持仓修复目录期间校验失败。') from None
        fresh = {item['report_key']: (raw, item) for raw, item in zip(current['item'], fresh_descriptors)}
        declared = {item['report_key']: (raw, item) for raw, item in zip(old_directory['item'], old_descriptors)}
        target = f'{repair.end_date.isoformat()}:{repair.report_type}'
        if target not in fresh:
            raise CollectionError('本次真实目录未确认目标报告，未使用旧目录回退。')
        # This minimum path cannot truthfully label newly advertised, uncollected
        # reports complete within a two-request approval. Stop rather than issue
        # extra requests, silently discard their availability, or fabricate a
        # failed HTTP request for an object we never attempted.
        if set(fresh) - set(declared) - {target}:
            raise CollectionError('本次目录出现未批准且未采集的其他报告。')
        if any(key != target and key in declared and item != declared[key][1]
               for key, (_, item) in fresh.items()):
            raise CollectionError('本次目录改变了未尝试历史报告的期间。')
        known = {row['report_key']: row for row in previous.get('item', [])}
        if len(known) != len(previous.get('item', [])) or set(known) - set(declared):
            raise CollectionError('原默认报告与声明目录无法核对。')
        descriptor = fresh[target][0]
        data = self.read(spec.interface, {'thscode': subject, 'end_date': repair.end_date.isoformat(),
                                          'report_type': repair.report_type})
        if data.get('thscode', subject) != subject:
            raise CollectionError('持仓修复响应主体与目标不一致。')
        wrapper = {'report_key': target, 'report': descriptor, 'data': data}
        try:
            portfolio_body(SimpleNamespace(dataset=spec.key, subject=subject),
                {'item': [wrapper], 'report_directory': {'item': [descriptor]},
                 'current_provider_directory': current})
        except NativeInputError:
            raise CollectionError('持仓修复响应未通过整组完整校验。') from None
        known[target] = wrapper
        declared[target] = fresh[target]
        return {**previous, 'item': [known[key] for key in sorted(known)],
                'report_directory': {**old_directory, 'item': [declared[key][0] for key in sorted(declared)]},
                'current_provider_directory': current}

    @staticmethod
    def end_day(now: datetime) -> date:
        local = now.astimezone(SHANGHAI)
        return local.date() if local.hour >= 20 else local.date() - timedelta(days=1)

    @staticmethod
    def merge_rows(old: dict | None, new: list[dict], field: str) -> list[dict]:
        result = {row[field]: row for row in (old or {}).get("item", [])}
        for row in new:
            result[row[field]] = row
        return [result[key] for key in sorted(result)]

    def bars(self, spec, subject, parameters, previous, now):
        end = parameters.end_date or self.end_day(now)
        old_rows = (previous or {}).get("item", [])
        full_start = parameters.start_date or (
            date.fromisoformat(previous["requested_start"]) if previous and previous.get("requested_start") else
            provider_date(old_rows[0]["date_ms"]) if old_rows else years_before(end, spec.years))
        start = full_start
        if old_rows and parameters.mode == "incremental" and parameters.start_date is None:
            # A calendar-day cushion avoids assuming a fixed holiday length.
            # The last ten stored trading dates additionally span long closures.
            start = min(provider_date(old_rows[max(0, len(old_rows) - 10)]["date_ms"]), end - timedelta(days=30))
        envelopes, by_date = [], {}

        def read_windows(request_years):
            # Every response retains its real returned-key receipt and passes
            # the same scope, date and value validation before publication.
            for a, b in windows(start, end, request_years, overlap_days=30 if spec.adjust == "forward" else 0):
                params = {"thscode": subject, "interval": "1d", "start": date_ms(a), "end": date_ms(b)}
                if spec.key == "stock_daily":
                    params["adjust"] = "none"
                if spec.key in ("etf_daily", "index_daily"):
                    # These four parameters are generated locally from the
                    # selected native identity and this exact window. Snapshot
                    # them before calling the client; never copy credentials,
                    # connection URLs, vendor messages or response payloads.
                    self.bar_request_context = {"interface": spec.interface,
                        "parameters": dict(params), "stage": "request"}
                data = self.read(spec.interface, params)
                if self.bar_request_context is not None:
                    self.bar_request_context["stage"] = "response_validation"
                if data.get("thscode", subject) != subject:
                    raise CollectionError("历史行情响应标的与请求不一致。")
                part = items(data, allow_empty=True)
                validate_bars(part, a, b)
                for row in part:
                    if row["date_ms"] in by_date and by_date[row["date_ms"]] != row:
                        raise CollectionError("分段行情的重叠日期不一致，未发布可能混合复权基准的版本。")
                    by_date[row["date_ms"]] = row
                envelopes.append({k: v for k, v in data.items() if k != "item"})

        read_windows(spec.years)
        if not by_date and spec.key == "index_daily" and (end - start).days >= 365:
            # The index endpoint can return an empty successful wide window
            # despite real bars in annual windows. Re-read the entire original
            # range in disjoint annual segments; a recent-only fallback would
            # silently shorten the historical scope. CollectionControl keeps
            # its existing request/time budgets and durable response journal,
            # so a yield resumes these exact segments without publishing a
            # partial head. A genuinely empty range still fails below.
            read_windows(1)
        if self.bar_request_context is not None:
            # Empty history and forward-adjustment completeness are aggregate
            # checks. The last window supplies context, not a claim that the
            # transport rejected that window or that earlier dates were absent.
            self.bar_request_context["stage"] = "history_validation"
            self.bar_request_context["validation_range"] = {
                "start_date": start.isoformat(), "end_date": end.isoformat()}
        rows = [by_date[key] for key in sorted(by_date)]
        if not rows:
            raise CollectionError("历史行情为空，尚不能确认该范围覆盖。")
        if spec.adjust == "forward" and old_rows and start > full_start:
            known = {r["date_ms"]: r for r in old_rows}
            if any(r["date_ms"] in known and r != known[r["date_ms"]] for r in rows):
                # Never splice two forward-adjustment bases. A failed full
                # refetch leaves the previous complete version visible.
                full = parameters.model_copy(update={"mode": "reconcile", "start_date": full_start})
                return self.bars(spec, subject, full, previous, now)
        if spec.adjust == "forward" and start <= full_start and old_rows:
            returned_dates = {r["date_ms"] for r in rows}
            required_dates = {r["date_ms"] for r in old_rows if start <= provider_date(r["date_ms"]) <= end}
            if not required_dates <= returned_dates:
                raise CollectionError("前复权历史重采缺少已有日期，未发布不完整或混合基准版本。")
        merged = self.merge_rows(previous, rows, "date_ms")
        self.bar_request_context = None
        return {"item": merged, "adjust": spec.adjust or "not_applicable",
                "requested_start": full_start.isoformat(), "requested_end": end.isoformat(),
                "coverage": "observed_rows_only", "provider_envelopes": envelopes}

    def nav(self, spec, subject, parameters, previous, now):
        if parameters.start_date or parameters.end_date:
            raise CollectionError("净值接口仅支持固定最近窗口，不能指定历史起止日期。")
        full = not previous or parameters.mode == "reconcile"
        data = self.read(spec.interface, {"thscode": subject, "range": "fyear" if full else "month", "nav_type": "unit,adj"})
        rows = items(data)
        today = now.astimezone(SHANGHAI).date()
        validate_bars(rows, years_before(today, 5) - timedelta(days=7), today, "nav_date")
        # A revised adjusted NAV may alter the whole sequence just like a
        # forward-adjusted price. Refresh the available history before publish.
        known = {r["nav_date"]: r for r in (previous or {}).get("item", [])}
        if not full and any(r["nav_date"] in known and r != known[r["nav_date"]] for r in rows):
            return self.nav(spec, subject, parameters.model_copy(update={"mode": "reconcile"}), previous, now)
        # A full rolling-window NAV response defines its own adjustment basis.
        # Earlier versions remain available by ID; do not attach older adjusted
        # values that the provider can no longer reconcile in the five-year API.
        return {**data, "item": sorted(rows, key=lambda r: r["nav_date"]) if full else self.merge_rows(previous, rows, "nav_date"),
                "coverage": "provider_rolling_window",
                "observed_start": min(provider_date(r["nav_date"]) for r in rows).isoformat(),
                "observed_end": max(provider_date(r["nav_date"]) for r in rows).isoformat()}

    def financials(self, spec, subject, parameters, previous, now):
        end = parameters.end_date or self.end_day(now)
        full_start = parameters.start_date or years_before(end, 10)
        if parameters.start_date is None and previous:
            # The ordinary two-year refresh merges older reports into its head.
            # Preserve their historical boundary even if old collector metadata
            # already drifted forward. Reconciliation must actually request the
            # oldest retained report, rather than silently keeping it forever
            # outside the moving ten-year window without a new source receipt.
            starts = [full_start]
            if previous.get("requested_start"):
                starts.append(date.fromisoformat(previous["requested_start"]))
            if previous.get("item"):
                starts.append(min(provider_date(row["period_end_ms"]) for row in previous["item"]))
            full_start = min(starts)
        start = full_start
        if previous and parameters.mode == "incremental" and parameters.start_date is None:
            # Keep the small recent request scope separate from the historical
            # head boundary. Actual returned-key receipts alone reconfirm rows;
            # neither this metadata nor a requested range invents confirmation.
            start = years_before(end, 2)
        rows = []
        for a, b in windows(start, end, 10):
            data = self.read(spec.interface, {"thscode": subject, "period": "quarterly", "start": date_ms(a), "end": date_ms(b)})
            for row in items(data, allow_empty=True):
                day = provider_date(row.get("period_end_ms"))
                if row.get("thscode") != subject or not a <= day <= b:
                    raise CollectionError("财务报表标的或报告期与请求不一致。")
                # Validate but do not reinterpret disclosure time as period end.
                provider_date(row.get("report_date_ms"))
                rows.append(row)
        if not rows:
            raise CollectionError("财务报表为空，尚不能确认数据已披露。")
        if len({r["period_end_ms"] for r in rows}) != len(rows):
            raise CollectionError("财务报表包含重复报告期。")
        return {"item": self.merge_rows(previous, rows, "period_end_ms"),
                "period": "quarterly", "historical_revision_evidence": False,
                "requested_start": full_start.isoformat(), "requested_end": end.isoformat()}

    def indicators(self, spec, subject, parameters, previous, now):
        end = parameters.end_date or self.end_day(now)
        start = parameters.start_date or years_before(end, 10 if not previous or parameters.mode == "reconcile" else 2)
        rows = []
        reports = set()
        for year in range(start.year, end.year + 1):
            for quarter, month, day in ((1, 3, 31), (2, 6, 30), (3, 9, 30), (4, 12, 31)):
                if not start <= date(year, month, day) <= end:
                    continue
                reports.add(f"{year}-{quarter}")
        # Failed old reports must remain eligible after a ten-year bootstrap
        # switches to the normal eight-quarter refresh window.
        reports.update(f["parameters"]["report"] for f in (previous or {}).get("failed_requests", [])
                       if "report" in f.get("parameters", {}))
        for report in sorted(reports):
            data = self.report_read(spec.interface, {"thscode": subject, "report": report})
            if data is None:
                continue
            if data.get("thscode") != subject or data.get("report") != report or not isinstance(data.get("abilities"), list):
                raise CollectionError("财务指标标的、报告期或结构与请求不一致。")
            rows.append(data)
        if not rows and not self.failures:
            raise CollectionError("请求范围没有已结束报告期。")
        return {"item": self.merge_rows(previous, rows, "report"), "historical_revision_evidence": False,
                "requested_start": start.isoformat(), "requested_end": end.isoformat()}

    def reports(self, spec, subject, parameters, previous):
        directory_key = spec.interface.replace("-history", "-report-dates")
        directory = self.read(directory_key, {"thscode": subject})
        dates = list(items(directory, allow_empty=True))
        known = {r["report_key"]: r for r in (previous or {}).get("item", [])}
        pending = {f"{f['parameters'].get('end_date')}:{f['parameters'].get('report_type')}"
                   for f in (previous or {}).get("failed_requests", [])}
        visible = {f"{provider_date(r['end_date_ms']).isoformat()}:{r.get('report_type')}" for r in dates}
        # A temporarily omitted directory entry must not erase an outstanding
        # report retry. Reuse the previously observed report descriptor.
        for report in (previous or {}).get("report_directory", {}).get("item", []):
            key = f"{provider_date(report['end_date_ms']).isoformat()}:{report.get('report_type')}"
            if key in pending and key not in visible:
                dates.append(report)
                visible.add(key)
        from app.data_ingestion.tonghuashun.control import control
        monitor = control()
        if monitor:
            monitor.emit(reports_total=len(dates), reports_saved=len(set(known) & visible))
        seen = set()
        for report in dates:
            kind = report.get("report_type")
            end = provider_date(report.get("end_date_ms"))
            if not isinstance(kind, str) or not kind or len(kind) > 40:
                raise CollectionError("持仓报告类型不正确。")
            key = f"{end.isoformat()}:{kind}"
            if key in seen:
                raise CollectionError("持仓报告目录包含重复报告期。")
            seen.add(key)
            if parameters.start_date and end < parameters.start_date or parameters.end_date and end > parameters.end_date:
                continue
            # Re-read all returned reports during reconciliation. Incremental
            # reads retain old reports and refresh the most recent year.
            newest = max((provider_date(r["end_date_ms"]) for r in dates), default=end)
            if key in known and key not in pending and parameters.mode == "incremental" and end < years_before(newest, 1):
                continue
            data = self.report_read(spec.interface, {"thscode": subject, "report_type": kind, "end_date": end.isoformat()})
            if data is None:
                continue
            items(data, allow_empty=True)
            known[key] = {"report_key": key, "report": report, "data": data}
            if monitor:
                monitor.emit(reports_saved=len(set(known) & visible), report_period=end.isoformat())
        return {"item": [known[k] for k in sorted(known)], "report_directory": {**directory, "item": dates},
                "current_provider_directory": directory, "historical_revision_evidence": False}
