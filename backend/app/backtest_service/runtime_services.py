"""Code-owned DB/API -> D04 -> D12 composition, no legacy/synthetic fallback."""

from .runtime_contracts import RuntimeFault


def create_services(*, settings=None, engine=None, provider=None, daemon=None):
    from app.core.config import get_settings
    from app.db.session import get_engine
    from .process import DockerDaemon, WorkerProcess, WorkRoot
    from .repository import DatabaseRunRepository
    from .service import FormalProvider
    from .supervisor import DataRunServices

    settings = settings or get_settings()
    if not settings.s3_work_root or not settings.s3_daemon_work_root or not settings.s3_worker_image:
        raise RuntimeFault("CAPABILITY_UNAVAILABLE", "可信runner工作根映射与预构建镜像尚未配置，拒绝接单")
    engine = engine or get_engine()
    repository = DatabaseRunRepository(engine, result_root=settings.s3_result_root)
    process = WorkerProcess(daemon or DockerDaemon(), WorkRoot(settings.s3_work_root, settings.s3_daemon_work_root), settings.s3_worker_image)
    provider = provider or FormalProvider(settings)
    services = DataRunServices(provider.binding, settings.s3_result_root)
    return repository, process, services
