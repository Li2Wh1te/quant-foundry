"""D13 composition point, deliberately fails closed until DB/API admission exists.

create_services must return RunRepository, WorkerProcess and DataRunServices.
The runner imports this code-owned module; no environment-specified plugin,
legacy queue, checkpoint restore or in-process Python strategy fallback exists.
"""

from .runtime_contracts import RuntimeFault


def create_services():
    raise RuntimeFault(
        "CAPABILITY_UNAVAILABLE",
        "D13共享运行仓储与正式事实映射未接线，S3 runner拒绝接单；旧任务切换由P01协调",
    )
