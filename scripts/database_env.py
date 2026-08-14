from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile


DATABASE_DEFAULTS = {
    "QF_DATABASE_HOST": "127.0.0.1",
    "QF_DATABASE_PORT": "5432",
    "QF_DATABASE_USER": "postgres",
    "QF_DATABASE_NAME": "quant_foundry",
}
PASSWORD_KEY = "QF_DATABASE_PASSWORD"
LEGACY_URL_KEY = "QF_DATABASE_URL"
WEAK_PASSWORDS = {"", "change-me", "postgres"}
ENV_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def ensure_database_environment(env_path: Path, template_path: Path) -> bool:
    if not env_path.exists():
        shutil.copyfile(template_path, env_path)

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    values = _read_values(lines)
    password_generated = values.get(PASSWORD_KEY, "") in WEAK_PASSWORDS
    password = secrets.token_hex(32) if password_generated else values[PASSWORD_KEY]
    desired = {**DATABASE_DEFAULTS, PASSWORD_KEY: password}

    updated_lines: list[str] = []
    written: set[str] = set()
    for line in lines:
        match = ENV_ASSIGNMENT.match(line)
        if match is None:
            updated_lines.append(line)
            continue

        key = match.group(1)
        if key == LEGACY_URL_KEY:
            continue
        if key in desired:
            value = (
                desired[key]
                if key == PASSWORD_KEY or not values.get(key)
                else values[key]
            )
            updated_lines.append(f"{key}={value}\n")
            written.add(key)
            continue
        updated_lines.append(line)

    missing = [key for key in desired if key not in written]
    if missing:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("\n")
        updated_lines.append("# PostgreSQL database configuration.\n")
        updated_lines.extend(f"{key}={desired[key]}\n" for key in missing)

    _atomic_write(env_path, "".join(updated_lines))
    os.chmod(env_path, 0o600)
    return password_generated


def _read_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        match = ENV_ASSIGNMENT.match(line)
        if match is None:
            continue
        key = match.group(1)
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _atomic_write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary_file:
        temporary_file.write(content)
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize PostgreSQL .env settings")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    arguments = parser.parse_args()

    generated = ensure_database_environment(arguments.env, arguments.template)
    if generated:
        print(f"Generated a random PostgreSQL password in {arguments.env}")
    else:
        print(f"Using the existing PostgreSQL password in {arguments.env}")


if __name__ == "__main__":
    main()
