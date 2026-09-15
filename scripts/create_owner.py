"""
Create the first owner account for a tenant.

Run once after `alembic upgrade head`:

    python -m scripts.create_owner --tenant-id <uuid> --email you@example.com

The password is read from the VOXDESK_OWNER_PASSWORD environment variable, or
prompted for without echo. It is never passed as a command-line argument,
because argv is visible to every process on the machine and lands in shell
history.

No default credentials exist anywhere in this repository by design.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid

from sqlalchemy import func, select

from app.auth import password as pw
from app.auth.service import normalize_email
from app.db.models import Tenant, User, UserRole
from app.db.session import get_sessionmaker


async def create_owner(tenant_id: uuid.UUID, email: str, plaintext: str) -> int:
    maker = get_sessionmaker()
    async with maker() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            print(f"error: no tenant with id {tenant_id}", file=sys.stderr)
            return 2

        normalized = normalize_email(email)
        existing = (
            await session.execute(select(User).where(User.email == normalized))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"error: {normalized} is already registered", file=sys.stderr)
            return 3

        try:
            pw.validate_policy(plaintext, email=normalized)
        except pw.PasswordPolicyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 4

        user = User(
            tenant_id=tenant.id,
            email=normalized,
            full_name="",
            password_hash=pw.hash_password(plaintext),
            role=UserRole.OWNER,
            is_active=True,
        )
        session.add(user)
        await session.commit()

        owners = (
            await session.execute(
                select(func.count(User.id)).where(
                    User.tenant_id == tenant.id, User.role == UserRole.OWNER
                )
            )
        ).scalar_one()
        print(f"created owner {normalized} for tenant '{tenant.name}'")
        print(f"tenant now has {owners} owner(s)")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an owner user.")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--email", required=True)
    args = parser.parse_args()

    try:
        tenant_id = uuid.UUID(args.tenant_id)
    except ValueError:
        print("error: --tenant-id must be a UUID", file=sys.stderr)
        return 2

    plaintext = os.environ.get("VOXDESK_OWNER_PASSWORD")
    if not plaintext:
        plaintext = getpass.getpass("Password: ")
        if plaintext != getpass.getpass("Confirm: "):
            print("error: passwords do not match", file=sys.stderr)
            return 5

    return asyncio.run(create_owner(tenant_id, args.email, plaintext))


if __name__ == "__main__":
    raise SystemExit(main())