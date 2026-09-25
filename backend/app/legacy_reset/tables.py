"""Metadata for small current maintenance receipts, independent of old models."""
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

maintenance = Table('data_store_legacy_maintenance',metadata,
    Column('singleton',Integer,primary_key=True),
    Column('phase',String(24),nullable=False),
    Column('plan_hash',String(64)),
    Column('completed_json',JSONB,nullable=False),
    Column('files_started',Boolean,nullable=False),
    Column('updated_at',DateTime(timezone=True),nullable=False,server_default=func.clock_timestamp()))

task_state = Table('data_store_legacy_task_state',metadata,
    Column('task_id',UUID(as_uuid=True),primary_key=True),
    Column('task_type',String(64),nullable=False),
    Column('previous_state',String(16),nullable=False),
    Column('paused_version',Integer,nullable=False),
    Column('captured_at',DateTime(timezone=True),nullable=False,server_default=func.clock_timestamp()))

restrictions = Table('data_store_legacy_restrictions',metadata,
    Column('origin_key',String(128),primary_key=True),
    Column('dataset',String(128)),
    Column('scope_key',String(128)),
    Column('target_key',String(128)),
    Column('fields_json',Text,nullable=False),
    Column('reason',Text,nullable=False),
    Column('payload_json',Text,nullable=False),
    Column('located',Boolean,nullable=False),
    Column('captured_at',DateTime(timezone=True),nullable=False,server_default=func.clock_timestamp()))
