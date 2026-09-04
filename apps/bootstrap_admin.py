from __future__ import annotations

import argparse
import asyncio
import getpass

from tgcurator.application import get_settings
from tgcurator.application.admin import BootstrapAdminService, BootstrapAlreadyCompleteError
from tgcurator.infrastructure.database import AsyncDatabase, SqlAlchemyAdminBootstrapRepository
from tgcurator.infrastructure.security import Argon2idPasswordHasher


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the one permitted active TG VLM Curator administrator."
    )
    parser.add_argument("--username", help="Administrator username. Prompts when omitted.")
    return parser.parse_args()


async def run(username: str | None) -> int:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("TGCURATOR_DATABASE_URL is required for bootstrap")

    resolved_username = username or input("Administrator username: ")
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm administrator password: ")
    if password != confirmation:
        raise ValueError("password confirmation does not match")

    database = AsyncDatabase(settings.database_url)
    service = BootstrapAdminService(
        repository=SqlAlchemyAdminBootstrapRepository(database),
        password_hasher=Argon2idPasswordHasher(),
    )
    try:
        admin_id = await service.bootstrap(username=resolved_username, password=password)
    except BootstrapAlreadyCompleteError:
        print("Bootstrap was not applied: an active administrator already exists.")
        return 2
    finally:
        await database.dispose()

    print(f"Administrator bootstrap completed: {admin_id}")
    return 0


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run(args.username)))


if __name__ == "__main__":
    main()
