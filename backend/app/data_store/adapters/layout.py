"""Compile CODE-OWNED validated business models to scalar Arrow layouts.

This is a serialization codec, not automatic supplier-field mapping. Native
mapping is explicit in each adapter. Model-valued lists become member rows in
one atomic object; they never become large JSON/points columns. Tiny declared
primitive vectors retain a bounded, tagged JSON encoding. Numbers outside the
DuckDB DECIMAL range retain their existing exact decimal-text contract, without
claiming arithmetic on those strings.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import types
from typing import get_origin, get_args, Annotated, Union, Literal

import pyarrow as pa
from pydantic import BaseModel, AwareDatetime

from ..schema import DatasetSpec
from ..values import exact_decimal
from .contracts import native_json, loads
from .canonical import NativeInputError


def plain(t):
    while get_origin(t) is Annotated:
        t = get_args(t)[0]
    if get_origin(t) in (Union, types.UnionType):
        values = [plain(v) for v in get_args(t) if v is not type(None)]
        if len(values) == 1:
            return values[0]
    return t


def model_type(t):
    t = plain(t)
    return t if isinstance(t, type) and issubclass(t, BaseModel) else None


def column_name(index, name):
    key = f'f{index}_{name}'
    return key if len(key) <= 63 else key[:50] + '_' + hashlib.sha256(key.encode()).hexdigest()[:12]


def partition_for(key):
    # Calendar month (or a compact undated bucket), never one file per event.
    import re
    value = key[2]
    month = value[:7] if re.match(r'^\d{4}-\d{2}-\d{2}(?:$|:)', value) else 'undated'
    bucket = hashlib.sha256((key[0] + '\0' + key[1]).encode()).digest()[0] % 16
    return f'{month}.b{bucket:02}'


class Layout:
    def __init__(self, model: type[BaseModel]):
        self.model = model
        self.models = []
        self.columns = {}
        self.children = {}
        self.fields = []
        self._visit(model)

    def _visit(self, model):
        if model in self.models:
            return
        index = len(self.models)
        self.models.append(model)
        props = model.model_json_schema().get('properties', {})
        child_models = []
        for name, field in model.model_fields.items():
            t = plain(field.annotation)
            child = model_type(t)
            repeated = False
            if get_origin(t) is list:
                child = model_type(get_args(t)[0]); repeated = bool(child)
            if child:
                self.children[model, name] = (child, repeated)
                col = column_name(index, name+'_count')
                self.columns[model, name] = (col, pa.int64(), 'count')
                self.fields.append(pa.field(col, pa.int64(), metadata={b'path': f'{model.__name__}.{name}'.encode()}))
                child_models.append(child)
                continue
            if t is type(None):
                # Unavailable capabilities are explicit in the static registry.
                continue
            p = props.get(name, {})
            variants = p.get('anyOf', [p]); pattern = next((v.get('pattern') for v in variants if v.get('pattern')), '')
            meta = {b'path': f'{model.__name__}.{name}'.encode()}
            from .facts import Tick, IntradayBar
            if model in (Tick,IntradayBar) and name in ('event_ns','start_ns'):
                meta.update({b'unit':b'epoch_ns',b'timezone':b'UTC'})
            encoding = 'scalar'
            if t is date:
                arrow = pa.date32()
            elif t in (datetime, AwareDatetime):
                arrow = pa.timestamp('us', tz='UTC')
            elif t is Decimal:
                arrow = pa.decimal128(38, 18)
            elif t is bool or get_origin(t) is Literal and all(type(v) is bool for v in get_args(t)):
                arrow = pa.bool_()
            elif t is int or get_origin(t) is Literal and all(type(v) is int for v in get_args(t)):
                arrow = pa.int64()
            else:
                arrow = pa.string()
                if '\\.[0-9]' in pattern:
                    encoding = 'decimal_text'
                    meta.update(logical_type=b'exact_decimal_text', arithmetic=b'unsupported', max_utf8_bytes=b'256')
                elif get_origin(t) in (list, tuple, dict):
                    encoding = 'primitive_json'
                    meta.update(logical_type=b'declared_primitive_vector', max_utf8_bytes=b'8192')
                else:
                    meta[b'max_utf8_bytes'] = b'8192'
            col = column_name(index, name)
            self.columns[model, name] = col, arrow, encoding
            self.fields.append(pa.field(col, arrow, nullable=True, metadata=meta))
        for child in child_models:
            self._visit(child)

    def spec(self, name, rule, semantics, *, report=True):
        fields = [pa.field(k, pa.string(), nullable=False, metadata={b'max_utf8_bytes': b'256'})
                  for k in ('representation','subject','object_key','member_key')]
        fields += [pa.field('row_kind', pa.int32(), nullable=False),
                   pa.field('basis_ns', pa.int64(), nullable=False, metadata={b'unit':b'epoch_ns'}),
                   pa.field('basis_group', pa.string(), nullable=False, metadata={b'max_utf8_bytes':b'64'}),
                   pa.field('basis_token', pa.string(), nullable=False, metadata={b'max_utf8_bytes':b'64'}),
                   pa.field('value_hash', pa.string(), nullable=False, metadata={b'max_utf8_bytes':b'64'}),
                   pa.field('basis_valid', pa.bool_(), nullable=False),
                   pa.field('basis_state', pa.string(), nullable=False, metadata={b'max_utf8_bytes':b'16'})]
        fields += [pa.field('quality_json',pa.string(),nullable=True,metadata={b'max_utf8_bytes':b'32768'})]
        fields += self.fields
        return DatasetSpec(name, pa.schema(fields), ('representation','subject','object_key','member_key'),
                           rule, semantics, 3 if report else 0, 'month-subject16-v1', partition_for)

    def encode(self, body):
        value = self.model.model_validate(body)
        result = []
        def walk(model, obj, path):
            row = {'member_key': path, 'row_kind': self.models.index(model)}
            children = []
            for name in model.model_fields:
                if (model,name) not in self.columns:
                    continue
                col,t,encoding = self.columns[model,name]
                value = getattr(obj, name)
                child = self.children.get((model,name))
                if child:
                    cls,repeated = child
                    row[col] = -1 if value is None else len(value) if repeated else 1
                    if value is not None:
                        children.extend((cls,v, path+'/'+name+'/'+str(i).zfill(8))
                                        for i,v in enumerate(value if repeated else [value]))
                elif value is None:
                    row[col] = None
                elif encoding == 'primitive_json':
                    encoded = native_json(value)
                    if len(encoded.encode()) > 8192:
                        raise NativeInputError('SOURCE_BUDGET_EXCEEDED','字段向量超过有界编码预算。')
                    row[col] = encoded
                elif pa.types.is_decimal(t):
                    row[col] = exact_decimal(value, precision=t.precision, scale=t.scale)
                elif pa.types.is_timestamp(t):
                    if value.utcoffset() is None:
                        raise NativeInputError('SOURCE_SCHEMA_INVALID','时间字段缺少时区。')
                    row[col] = value.astimezone(timezone.utc)
                else:
                    row[col] = value
            result.append(row)
            if len(result)>100000:
                raise NativeInputError('SOURCE_BUDGET_EXCEEDED','完整业务对象超过成员预算。')
            for args in children:
                walk(*args)
        walk(self.model,value,'root')
        return tuple(result)

    def decode(self, rows):
        """Reconstruct one complete business object; missing children never vanish."""
        by_path = {r['member_key']:r for r in rows}
        if len(by_path)!=len(rows):
            raise NativeInputError('REPORT_INCOMPLETE','成员路径重复。')
        consumed=set()
        def read(model,path):
            row=by_path.get(path)
            if row is None or row['row_kind'] != self.models.index(model):
                raise NativeInputError('REPORT_INCOMPLETE','报告成员缺失或类型错误。')
            consumed.add(path); out={}
            for name in model.model_fields:
                if (model,name) not in self.columns:
                    out[name]=None; continue
                col,t,encoding=self.columns[model,name]; v=row[col]
                child=self.children.get((model,name))
                if child:
                    cls,repeated=child
                    if v == -1: out[name]=None
                    elif type(v) is not int or v<0 or v>100000:
                        raise NativeInputError('REPORT_INCOMPLETE','成员计数无效。')
                    else:
                        children=[read(cls,path+'/'+name+'/'+str(i).zfill(8)) for i in range(v)]
                        if not repeated and v!=1: raise NativeInputError('REPORT_INCOMPLETE','对象缺失。')
                        out[name]=children if repeated else children[0]
                elif encoding=='primitive_json' and v is not None: out[name]=loads(v)
                else: out[name]=v
            return out
        result=read(self.model,'root')
        if consumed != set(by_path): raise NativeInputError('REPORT_INCOMPLETE','报告存在未声明成员。')
        from ..schema import api_value
        def transport(value):
            if isinstance(value,dict):return {k:transport(v) for k,v in value.items()}
            if isinstance(value,(list,tuple)):return [transport(v) for v in value]
            return api_value(value)
        return transport(self.model.model_validate(result).model_dump(mode='python'))


def tick_partition(key):
    # Explicit source event sequence ranges avoid a single enormous hot month.
    # Channel/session are part of event identity, not guessed from nanoseconds.
    day,session,channel,sequence=key[2].split(':')
    bucket=hashlib.sha256((key[0]+'\0'+key[1]+'\0'+session+'\0'+channel).encode()).digest()[0]%16
    return f'{day}.b{bucket:02}.q{int(sequence)//100000:015}'
