"""Credential-free, bounded Parquet download, validation and restartable import.

Signed URLs exist only during download. Files are validated on temporary disk
before any source version is published. Staged per-symbol payloads survive a
worker restart and disappear in the same transaction as their publication.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

import pyarrow.parquet as pq
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.data_ingestion.clients.tonghuashun import TonghuashunError
from app.data_ingestion.models.tonghuashun import TonghuashunDumpImport as Import, TonghuashunDumpStage as Stage
from app.data_ingestion.tonghuashun.contracts import CollectionError, DATASETS, SHANGHAI, exact_json, provider_date, validate_bars
from app.data_ingestion.tonghuashun.repository import CollectionRepository

DUMPS = {
    'stock_daily_dump': ('a_share_daily_k_1d_none_10y', 'stock_daily', 'date_ms'),
    'stock_recent_dump': ('a_share_daily_k_1d_none_10d', 'stock_daily', 'date_ms'),
    'stock_actions_dump': ('a_share_adjustment_factors_event_none_all', 'stock_actions', 'ex_date_ms'),
}
MAX_BYTES = 2 * 1024**3
MAX_ROWS = 30_000_000


def download(url, path):
    """Resolve once and pin a public address while verifying the HTTPS hostname.

    This accepts public S3-compatible object stores without guessing a vendor
    bucket. It rejects credentials, local addresses and redirects; no source API
    headers, cookies, environment proxies or signed URL text enter logs.
    """
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None,443) or any(ord(c)<33 for c in url)):
            raise ValueError()
        host = parsed.hostname
        addresses = {a[4][0] for a in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise ValueError()
        addresses = sorted(addresses, key=lambda a: (ipaddress.ip_address(a).version, a))
    except (ValueError, TypeError, OSError):
        raise CollectionError('批量下载地址必须是合法的公网 HTTPS 对象地址。') from None
    from app.data_ingestion.tonghuashun.control import control
    monitor = control()
    last_progress = 0.0
    digest, size, deadline = hashlib.sha256(), 0, time.monotonic()+900
    connection = http.client.HTTPSConnection(host,443,timeout=20,context=ssl.create_default_context())
    # Keep TLS SNI/certificate validation tied to the original hostname while
    # connecting only to already-validated addresses. http.client does not log
    # request URLs (debuglevel stays zero), unlike urllib3 debug request logs.
    def connect_public_address(ignored, timeout, source_address=None):
        # Containers may resolve AAAA records without having an IPv6 route.
        # Try the validated IPv4 addresses first, with one bounded connection
        # budget. Never resolve the hostname again or fall back to a proxy.
        connect_deadline = time.monotonic() + timeout
        for address in addresses:
            remaining = connect_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return socket.create_connection((address, 443), min(5, remaining), source_address)
            except OSError:
                continue
        raise OSError("No validated public address is reachable")

    connection._create_connection = connect_public_address
    try:
        target = parsed.path or '/'
        if parsed.query:
            target += '?' + parsed.query
        connection.request('GET',target,headers={'Accept':'application/octet-stream'})
        response = connection.getresponse()
        try:
            if response.status in (401,403):
                raise CollectionError('批量文件下载授权已过期，下次将重新获取下载链接。')
            if response.status != 200:
                raise CollectionError('批量文件下载失败，未发布数据。')
            declared = response.headers.get('Content-Length')
            if declared and (not declared.isdigit() or int(declared)>MAX_BYTES):
                raise CollectionError('批量文件超过2GiB下载上限。')
            if monitor:
                monitor.emit(stage='下载 Parquet 文件', download_bytes=0, download_total=int(declared) if declared else None)
            with open(path,'wb') as file:
                while chunk := response.read1(64*1024):
                    size += len(chunk)
                    if monitor and time.monotonic() - last_progress >= 3:
                        monitor.emit(download_bytes=size)
                        last_progress = time.monotonic()
                    if size>MAX_BYTES or time.monotonic()>deadline:
                        raise CollectionError('批量文件下载超过大小或时间上限。')
                    file.write(chunk)
                    digest.update(chunk)
            if monitor:
                monitor.emit(download_bytes=size)
            if size == 0 or declared and size != int(declared):
                raise CollectionError('批量文件下载不完整。')
        finally:
            response.close()
    except CollectionError:
        raise
    except (http.client.HTTPException, OSError, ValueError):
        raise CollectionError('批量文件下载失败，原有数据未覆盖。') from None
    finally:
        connection.close()
    return digest.hexdigest(),size


def validate_file(path, dataset, directory, now):
    """Validate every row and uniqueness on disk before staging any publication."""
    _, _, date_key = DUMPS[dataset]
    disk = sqlite3.connect(str(Path(directory)/'rows.sqlite'))
    disk.execute('CREATE TABLE records (code TEXT, day INTEGER, payload TEXT, PRIMARY KEY(code,day))')
    try:
        # Keep decoding synchronous and close Arrow resources before the worker exits.
        with pq.ParquetFile(path) as file:
            count = file.metadata.num_rows
            if not 0 < count <= MAX_ROWS:
                raise CollectionError('批量文件为空或超过记录数上限。')
            required = {'thscode','currency',date_key}
            required |= ({'interval','adjusted','open_price','high_price','low_price','close_price','volume','turnover'}
                if date_key=='date_ms' else {'ticker','dividend_per_share','per_share_bonus','allotment_ratio','allotment_price'})
            if len(file.schema_arrow.names) != len(set(file.schema_arrow.names)) or not required <= set(file.schema_arrow.names):
                raise CollectionError('Parquet 字段与官方日线或复权事件结构不一致。')
            expanded, observed_min, observed_max = 0, None, None
            for batch in file.iter_batches(batch_size=4096, use_threads=False):
                payloads = []
                for row in batch.to_pylist():
                    code = row.get('thscode')
                    if not isinstance(code,str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',code) or row.get('currency')!='CNY':
                        raise CollectionError('批量文件包含非法代码或币种。')
                    day = provider_date(row.get(date_key))
                    if day > now.astimezone(SHANGHAI).date():
                        raise CollectionError('批量文件包含未来日期。')
                    if row[date_key] != int(datetime.combine(day,datetime.min.time(),SHANGHAI).timestamp()*1000):
                        raise CollectionError('批量文件日期不是上海时区零点。')
                    # Arrow decimals retain precision. Binary floats retain their
                    # shortest round-trip representation, without invented digits.
                    row = json.loads(exact_json(row),parse_float=Decimal)
                    if date_key=='date_ms':
                        if row['interval']!='1d' or row['adjusted']!='none':
                            raise CollectionError('批量日线不是未复权日线口径。')
                        validate_bars([row],day,day)
                    else:
                        for key in ('dividend_per_share','per_share_bonus','allotment_ratio','allotment_price'):
                            value = row[key]
                            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,Decimal)) or value<0):
                                raise CollectionError('复权事件包含非法数值。')
                    encoded = exact_json(row)
                    expanded += len(encoded)
                    if expanded > 8*1024**3:
                        raise CollectionError('批量文件解码后超过8GiB暂存上限。')
                    payloads.append((code,row[date_key],encoded))
                    observed_min = min(observed_min or day,day)
                    observed_max = max(observed_max or day,day)
                disk.executemany('INSERT INTO records VALUES (?,?,?)',payloads)
                disk.commit()
            return disk, {'rows':count,'observed_start':observed_min.isoformat(),'observed_end':observed_max.isoformat(),
                'columns':file.schema_arrow.names, 'numeric_encoding':'provider_decimal_or_float_roundtrip'}
    except CollectionError:
        disk.close()
        raise
    except Exception:
        # Arrow/SQLite errors may include filenames or provider content. Only
        # this authored message is allowed into scheduler/operator diagnostics.
        disk.close()
        raise CollectionError('Parquet 解析、类型或重复主键校验失败，未发布数据。') from None


def reserve(dataset,params,engine,now):
    from app.data_ingestion.tonghuashun.service import aware
    with Session(engine) as session:
        insert = pg_insert if engine.dialect.name=='postgresql' else sqlite_insert
        session.execute(insert(Import).values(dataset=dataset,generation=uuid4(),status='new',
            started_at=now,lease_until=now,metadata_json='{}',total_subjects=0,imported_subjects=0,superseded_subjects=0).on_conflict_do_nothing())
        slot = session.scalar(select(Import).where(Import.dataset==dataset).with_for_update())
        if slot.status=='ready':
            return slot.generation,False
        if slot.status=='downloading' and aware(slot.lease_until)>now:
            return None,False
        if slot.status=='completed' and not params.refresh_today:
            cutoff = now.astimezone(SHANGHAI).replace(hour=20,minute=30,second=0,microsecond=0)
            if params.mode=='backfill' or dataset=='stock_daily_dump' and params.mode!='reconcile' or aware(slot.completed_at)>=cutoff:
                return None,False
        if dataset!='stock_daily_dump' and now.astimezone(SHANGHAI).hour*60+now.astimezone(SHANGHAI).minute < 1230 and not params.refresh_today:
            return None,False
        session.execute(delete(Stage).where(Stage.dataset==dataset))
        slot.generation,slot.status,slot.started_at = uuid4(),'downloading',now
        slot.lease_until = now+timedelta(hours=2)
        slot.imported_subjects=slot.superseded_subjects=slot.total_subjects=0
        slot.completed_at=None
        generation=slot.generation
        session.commit()
        return generation,True


def stage_file(dataset,generation,client,engine,now,downloader=download):
    spec=DATASETS[dataset]
    # The transport exposes the descriptor in memory only. Do not pass it to
    # Acquisition, which would otherwise retain a signed URL in source data.
    from app.data_ingestion.tonghuashun.control import control
    monitor = control()
    if monitor:
        monitor.check()
        monitor.emit(stage='获取文件下载信息')
    response=client.request(spec.interface,{})
    descriptor=response.data
    expected=DUMPS[dataset][0]
    if descriptor.get('dump_id',expected)!=expected:
        raise CollectionError('导出文件类型与请求不一致。')
    url=descriptor.get('presigned_url')
    if not isinstance(url,str):
        raise CollectionError('官方导出响应缺少 presigned_url。')
    with tempfile.TemporaryDirectory(prefix='qf-ths-dump-') as directory:
        path=Path(directory)/'data.parquet'
        digest,size=downloader(url,path)
        advertised=descriptor.get('sha256')
        if advertised is not None and advertised!=digest:
            raise CollectionError('导出文件摘要与提供方声明不一致。')
        if monitor:
            monitor.emit(stage="校验 Parquet 文件")
        disk,metadata=validate_file(path,dataset,directory,now)
        try:
            metadata.update({'sha256':digest,'download_bytes':size,'dump_id':expected,'request_id':response.request_id})
            # No arbitrary descriptor fields, object keys or URLs are persisted.
            with Session(engine) as session:
                slot=session.scalar(select(Import).where(Import.dataset==dataset).with_for_update())
                if slot.generation!=generation or slot.status!='downloading':
                    raise CollectionError('批量导入租约已被替换，本次暂存未发布。')
                if monitor:
                    monitor.emit(stage="暂存已校验文件")
                count=0
                for (code,) in disk.execute('SELECT DISTINCT code FROM records ORDER BY code'):
                    payload='{"item":['+','.join(r[0] for r in disk.execute('SELECT payload FROM records WHERE code=? ORDER BY day',(code,)))+']}'
                    session.add(Stage(dataset=dataset,subject=code,data_json=payload))
                    count+=1
                    if count%100==0:
                        session.flush()
                slot.digest,slot.metadata_json,slot.total_subjects=digest,exact_json(metadata),count
                slot.status='ready'
                session.commit()
        finally:
            disk.close()


def publish_batch(dataset,generation,params,engine,now):
    from app.data_ingestion.tonghuashun.service import aware
    _,target,date_key=DUMPS[dataset]
    with Session(engine) as session:
        slot=session.scalar(select(Import).where(Import.dataset==dataset).with_for_update())
        if slot.generation!=generation or slot.status!='ready':
            return {'imported':0,'pending':0,'superseded':0}
        metadata=json.loads(slot.metadata_json)
        rows=session.scalars(select(Stage).where(Stage.dataset==dataset).order_by(Stage.subject).limit(params.batch_size)).all()
        superseded,changed,unchanged,received=0,0,0,0
        repo=CollectionRepository(session)
        for staged in rows:
            old=repo.read(target,staged.subject,'default')
            new=json.loads(staged.data_json,parse_float=Decimal)['item']
            known={r[date_key]:r for r in (old.data or {}).get('item',[])}
            newer=old.succeeded_at is not None and aware(old.succeeded_at)>aware(slot.started_at)
            # A full bootstrap only fills missing dates in established history.
            # Recent dumps can revise overlaps unless a newer REST run won first.
            fill_only=newer or dataset=='stock_daily_dump' and params.mode!='reconcile'
            merged={r[date_key]:r for r in new}
            if fill_only:
                merged.update(known)
            else:
                merged={**known,**merged}
            superseded+=int(newer)
            data={**(old.data or {}),'item':[merged[k] for k in sorted(merged)],
                'adjust':'none','bulk_source':metadata,'coverage':'observed_rows_only',
                'requested_start':min(metadata['observed_start'],(old.data or {}).get('requested_start',metadata['observed_start'])),
                'requested_end':max(metadata['observed_end'],(old.data or {}).get('requested_end',metadata['observed_end']))}
            if target=='stock_actions':
                data['thscode']=staged.subject
            result=repo.publish(target,staged.subject,'default',expected=old.revision,data=data,
                requests=[{'interface':DATASETS[dataset].interface,'parameters':{},'artifact_sha256':slot.digest}],now=now)
            changed+=result['changed'];unchanged+=result['unchanged'];received+=len(new)
            session.delete(staged)
        slot.imported_subjects+=len(rows)
        slot.superseded_subjects+=superseded
        pending=slot.total_subjects-slot.imported_subjects
        if not pending:
            slot.status,slot.completed_at='completed',now
        previous=repo.read(dataset,'market','default')
        manifest={'item':[],'artifact':metadata,'total_subjects':slot.total_subjects,
            'imported_subjects':slot.imported_subjects,'superseded_subjects':slot.superseded_subjects,
            'requested_start':metadata['observed_start'],'requested_end':metadata['observed_end'],
            'failed_requests':[{'reason':'batch_budget','pending':pending}] if pending else []}
        result=repo.publish(dataset,'market','default',expected=previous.revision,data=manifest,requests=[],now=now)
        session.commit()
        return {'imported':len(rows),'pending':pending,'superseded':superseded,'changed':changed,
            'unchanged':unchanged,'received':received,'version_id':result['version_id'],
            'start':metadata['observed_start'],'end':metadata['observed_end']}


def collect_dump(dataset,params,raw_client,engine,*,now=None):
    from app.data_ingestion.tonghuashun.service import BudgetedClient, logger
    now=now or datetime.now(UTC)
    spec=DATASETS[dataset]
    from app.data_ingestion.tonghuashun.control import control
    monitor = control()
    if monitor:
        monitor.check()
    generation,needs_download=reserve(dataset,params,engine,now)
    if generation is None:
        return {'event':'tonghuashun_collection_completed','source':'tonghuashun','dataset':dataset,
            'message':f'{spec.name}本次跳过：当前导入已有运行占用、本期已完成或尚未到更新时间；完成标记未推进。'}
    try:
        if needs_download:
            stage_file(dataset,generation,BudgetedClient(raw_client,engine),engine,now)
        if monitor:
            monitor.check()
            monitor.emit(stage='分批入库')
        result=publish_batch(dataset,generation,params,engine,datetime.now(UTC))
        if monitor:
            with Session(engine) as session:
                slot = session.get(Import, dataset)
                monitor.emit(stage='本批入库完成', batch_total=result['imported'], processed=result['imported'],
                    succeeded=result['imported'], received=result['received'], changed=result['changed'],
                    coverage_total=slot.total_subjects, coverage_pending=result['pending'],
                    last_advanced_at=datetime.now(UTC).isoformat())
    except Exception as exc:
        with Session(engine) as session:
            slot=session.scalar(select(Import).where(Import.dataset==dataset).with_for_update())
            if slot.generation==generation and slot.status=='downloading':
                slot.status='failed'
            session.commit()
        # Never chain network/Arrow exceptions containing signed URLs or data.
        message=str(exc) if isinstance(exc,(CollectionError,TonghuashunError)) else '批量导入失败，未完成范围将在下次续作。'
        logger.warning('tonghuashun_collection_failed',source='tonghuashun',data_type=dataset,
            message=f'{spec.name}失败：文件请求范围，失败1批，完成标记未推进；{message}',error_type=type(exc).__name__)
        raise CollectionError(message) from None
    message=(f'{spec.name}本批完成：日期范围 {result.get("start","文件范围")} 至 {result.get("end","文件范围")}，'
        f'导入 {result["imported"]} 个标的，拉取 {result.get("received",0)} 条，变更 {result.get("changed",0)} 条，'
        f'未变更 {result.get("unchanged",0)} 条，失败0批，待导入 {result["pending"]} 个；'
        + ('本批标的完成标记已推进，全文件尚未完成。' if result['pending'] else '全文件完成标记已推进。'))
    logger.info('tonghuashun_collection_completed',source='tonghuashun',data_type=dataset,message=message,**result)
    return {'event':'tonghuashun_collection_completed','source':'tonghuashun','dataset':dataset,'message':message,**result}
