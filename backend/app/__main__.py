import uvicorn

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=str(settings.server_host),
        port=settings.server_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
