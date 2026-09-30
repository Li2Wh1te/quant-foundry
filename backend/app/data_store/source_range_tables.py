"""Current coalesced capture metadata, separate from the frozen store kernel."""
from sqlalchemy import MetaData,Table,Column,Text,Boolean,DateTime,Integer,BigInteger,CheckConstraint,Index,text
from sqlalchemy.dialects.postgresql import JSONB

metadata=MetaData()
ranges=Table('data_store_source_ranges',metadata,
    *(Column(k,Text,primary_key=True) for k in ('source','dataset','subject','variant','range_key')),
    Column('pending',Boolean,nullable=False,server_default=text('true')),
    Column('bootstrap_pending',Boolean,nullable=False,server_default=text('false')),
    Column('enqueued_at',DateTime(timezone=True),nullable=False,server_default=text('transaction_timestamp()')),
    Column('lower_at',DateTime(timezone=True),nullable=False,server_default=text("'-infinity'")),
    Column('active',JSONB),
    Column('change_revision',BigInteger,nullable=False,server_default=text('0')),
    CheckConstraint('change_revision>=0',name='data_store_source_ranges_change_revision_check'))
Index('ix_data_store_ranges_work',ranges.c.source,ranges.c.dataset,
      ranges.c.enqueued_at,ranges.c.range_key,ranges.c.subject,ranges.c.variant)
Index('ix_data_store_ranges_active',ranges.c.source,ranges.c.dataset,postgresql_where=text('active IS NOT NULL'))
version=Table('data_store_capture_version',metadata,
    Column('singleton',Integer,primary_key=True,autoincrement=False),Column('version',Integer,nullable=False),
    CheckConstraint('singleton=1',name='data_store_capture_version_singleton_check'))
