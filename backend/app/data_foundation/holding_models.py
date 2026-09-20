"""Typed report heads and members, sharing work, decisions and release roots.

Each head is sealed in its creating transaction. Ordinals preserve source
occurrences, including duplicate securities; an official manifest references
one head, never a mixture of member revisions.
"""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.data_foundation.models import Record, fk


class ReportFields:
    fund_share_id: Mapped[UUID] = mapped_column(ForeignKey('instruments.id', ondelete='RESTRICT'))
    period_start: Mapped[date]
    period_end: Mapped[date]
    report_type: Mapped[str] = mapped_column(String(16))
    scope_kind: Mapped[str] = mapped_column(String(24))
    series: Mapped[str] = mapped_column(String(128))
    member_count: Mapped[int] = mapped_column(Integer)
    transport_complete: Mapped[bool]
    portfolio_complete: Mapped[bool | None]
    public_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def head_checks():
    return (CheckConstraint('period_start <= period_end AND member_count >= 0', name='report_range'),
            CheckConstraint("report_type IN ('quarter','annual','semiannual')", name='report_type'),
            CheckConstraint("scope_kind IN ('provider_reported','top_n','full_portfolio')", name='report_scope'))


class CandidateReport(ReportFields, Base):
    __tablename__ = 'foundation_report_candidates'
    candidate_id: Mapped[UUID] = mapped_column(fk('candidates'), primary_key=True)
    __table_args__ = head_checks()


class MemberFields:
    member_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_instrument_id: Mapped[UUID] = mapped_column(ForeignKey('instruments.id', ondelete='RESTRICT'))
    binding_id: Mapped[UUID] = mapped_column(fk('source_bindings'))
    source_member_code: Mapped[str] = mapped_column(String(64))
    hold_ratio: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(32, 8))
    period_change_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    rank: Mapped[int | None]
    field_quality_json: Mapped[str] = mapped_column(Text)


def member_checks():
    return (CheckConstraint("member_ordinal >= 0 AND hold_ratio >= 0 AND hold_ratio <= 1 "
                            "AND (rank IS NULL OR rank > 0) "
                            "AND (market_value IS NULL OR (market_value >= 0 AND market_value < 'Infinity')) "
                            "AND (period_change_ratio IS NULL OR (period_change_ratio > '-Infinity' AND period_change_ratio < 'Infinity'))",
                            name='holding_values'),)


class CandidateReportMember(MemberFields, Base):
    __tablename__ = 'foundation_report_candidate_members'
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey('foundation_report_candidates.candidate_id', ondelete='RESTRICT'), primary_key=True)
    __table_args__ = member_checks()


class OfficialReport(Record, ReportFields, Base):
    __tablename__ = 'foundation_report_official_revisions'
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey('foundation_report_candidates.candidate_id', ondelete='RESTRICT'))
    decision_id: Mapped[UUID] = mapped_column(fk('decisions'), unique=True)
    values_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = head_checks()


class OfficialReportMember(MemberFields, Base):
    __tablename__ = 'foundation_report_official_members'
    official_id: Mapped[UUID] = mapped_column(fk('report_official_revisions'), primary_key=True)
    __table_args__ = member_checks()


class ReportBlockMember(Base):
    __tablename__ = 'foundation_report_block_members'
    block_id: Mapped[UUID] = mapped_column(fk('release_blocks'), primary_key=True)
    target_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    fund_share_id: Mapped[UUID] = mapped_column(ForeignKey('instruments.id', ondelete='RESTRICT'))
    period_start: Mapped[date]
    period_end: Mapped[date]
    report_type: Mapped[str] = mapped_column(String(16))
    scope_kind: Mapped[str] = mapped_column(String(24))
    series: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))
    official_id: Mapped[UUID | None] = mapped_column(fk('report_official_revisions'))
    decision_id: Mapped[UUID] = mapped_column(fk('decisions'))
    __table_args__ = (CheckConstraint("(state = 'value' AND official_id IS NOT NULL) OR "
        "(state IN ('gap','blocked','withdrawn') AND official_id IS NULL)", name='report_member_state'),)


class ReportIssueTarget(Base):
    """Restrict a common issue to one report key and optionally one revision."""
    __tablename__ = 'foundation_report_issue_targets'
    issue_revision_id: Mapped[UUID] = mapped_column(fk('issue_revisions'), primary_key=True)
    target_key: Mapped[str] = mapped_column(String(64))
    official_id: Mapped[UUID | None] = mapped_column(fk('report_official_revisions'))
