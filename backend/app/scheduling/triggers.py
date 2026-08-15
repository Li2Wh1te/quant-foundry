from datetime import UTC
from zoneinfo import ZoneInfo

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.scheduling.schemas import (
    CronSchedule,
    IntervalSchedule,
    OnceSchedule,
    ScheduleConfig,
)


def build_trigger(schedule: ScheduleConfig) -> BaseTrigger:
    if isinstance(schedule, CronSchedule):
        return CronTrigger.from_crontab(
            schedule.expression,
            timezone=ZoneInfo(schedule.timezone),
        )
    if isinstance(schedule, IntervalSchedule):
        return IntervalTrigger(
            seconds=schedule.seconds,
            start_date=schedule.start_at,
            timezone=UTC,
        )
    if isinstance(schedule, OnceSchedule):
        return DateTrigger(run_date=schedule.run_at, timezone=UTC)
    raise TypeError(f"unsupported schedule type: {type(schedule)!r}")
