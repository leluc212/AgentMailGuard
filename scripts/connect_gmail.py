"""Register the Gmail test account as a watched mailbox (specs/tasks.md 6.10; R17.1, R2.1).

Owner-run on the host after `make up` and `make seed`, with a fresh GMAIL_ACCESS_TOKEN in .env
(docs/demo-runbook.md §3.2–3.3):

    make connect-gmail ADDRESS=ragemail.demo.<you>@gmail.com

What it does, stopping at the first failure:
  1. Reads GMAIL_ACCESS_TOKEN from the environment or .env. It never prints the token.
  2. Calls Gmail users.getProfile and refuses a token that belongs to another account.
  3. Upserts the mailbox into the demo tenant (Acme) with provider gmail,
     credentials_ref env:GMAIL_ACCESS_TOKEN and status active.
  4. Sets the mailbox checkpoint to the account's current historyId, so the first sync
     imports only mail that arrives from now on (not the last 50 messages, and never the
     literal 'initial' cursor that the initial-sync path would store).
Re-running it is safe. It also re-activates a mailbox that a 401 marked needs_reauth, and it
moves the start point to "now".
No push notifications reach localhost, so new mail is pulled with
POST /v1/mailboxes/<id>/resync (runbook §6 step 4; scripts/phase6_gate.py does it for you).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from dotenv import dotenv_values

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import DEMO_ORG_ID

GMAIL_TOKEN_VAR = "GMAIL_ACCESS_TOKEN"
CREDENTIALS_REF = f"env:{GMAIL_TOKEN_VAR}"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


class ConnectError(RuntimeError):
    """Connecting the mailbox failed; the message says why and what to do."""


def resolve_gmail_token(environ: Mapping[str, str], dotenv: Mapping[str, str | None]) -> str:
    """The token from the process environment, else from .env; blank counts as missing."""
    for source in (environ.get(GMAIL_TOKEN_VAR), dotenv.get(GMAIL_TOKEN_VAR)):
        if source and source.strip():
            return source.strip()
    raise ConnectError(
        f"{GMAIL_TOKEN_VAR} is not set: mint a token (docs/demo-runbook.md §3.2) "
        "and put it in .env (§3.3)"
    )


async def fetch_profile(http: httpx.AsyncClient, token: str) -> dict[str, Any]:
    """Gmail users.getProfile for the token's account."""
    resp = await http.get(PROFILE_URL, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        raise ConnectError(
            "Gmail says the access token is invalid or expired (HTTP 401): mint a new one "
            "(docs/demo-runbook.md §3.2), update .env, then run make up"
        )
    if resp.status_code != 200:
        raise ConnectError(f"Gmail getProfile returned HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if not isinstance(body, dict):
        raise ConnectError(f"Gmail getProfile returned a non-object body: {body!r}")
    return body


def check_profile(profile: Mapping[str, Any], address: str) -> str:
    """The token must belong to `address`; returns the mailbox's current historyId."""
    owner = str(profile.get("emailAddress", "")).strip().lower()
    if owner != address.strip().lower():
        raise ConnectError(
            f"the token belongs to {owner or '<unknown>'}, not {address}: sign in to the "
            "OAuth Playground as the test account only (docs/demo-runbook.md §3.2)"
        )
    history_id = str(profile.get("historyId") or "").strip()
    if not history_id:
        raise ConnectError("Gmail getProfile returned no historyId")
    return history_id


async def register_mailbox(
    pool: asyncpg.Pool[Any], *, organization_id: UUID, address: str, history_id: str
) -> UUID:
    """Upsert the mailbox (active, env credentials) and point its checkpoint at `history_id`."""
    normalized = address.strip().lower()
    async with pool.acquire() as conn, conn.transaction():
        org = await conn.fetchval("SELECT id FROM organization WHERE id = $1", organization_id)
        if org is None:
            raise ConnectError(
                f"organization {organization_id} does not exist: run make seed first"
            )
        mailbox_id = await conn.fetchval(
            """
            INSERT INTO mailbox (
                id, organization_id, provider, address, display_name, status, credentials_ref
            ) VALUES (gen_random_uuid(), $1, 'gmail', $2, 'Gmail test account', 'active', $3)
            ON CONFLICT (organization_id, address) DO UPDATE SET
                provider = 'gmail',
                status = 'active',
                credentials_ref = EXCLUDED.credentials_ref
            RETURNING id
            """,
            organization_id,
            normalized,
            CREDENTIALS_REF,
        )
        await conn.execute(
            """
            INSERT INTO mailbox_checkpoint (
                mailbox_id, organization_id, history_id, sync_state, last_sync_at,
                pending_followup
            ) VALUES ($1, $2, $3, 'idle', now(), false)
            ON CONFLICT (mailbox_id) DO UPDATE SET
                history_id = EXCLUDED.history_id,
                sync_state = 'idle',
                last_sync_at = now(),
                pending_followup = false
            WHERE mailbox_checkpoint.organization_id = EXCLUDED.organization_id
            """,
            mailbox_id,
            organization_id,
            history_id,
        )
    return UUID(str(mailbox_id))


async def run(address: str, organization_id: UUID) -> UUID:
    token = resolve_gmail_token(os.environ, dotenv_values(".env"))
    async with httpx.AsyncClient(timeout=10.0) as http:
        history_id = check_profile(await fetch_profile(http, token), address)
    print(f"ok   token belongs to {address}; current historyId {history_id}")
    settings = AppSettings()
    pool = await create_pool_from_settings(settings.database)
    try:
        mailbox_id = await register_mailbox(
            pool, organization_id=organization_id, address=address, history_id=history_id
        )
    finally:
        await pool.close()
    print(
        f"ok   mailbox {mailbox_id} in organization {organization_id}: provider gmail, "
        f"credentials_ref {CREDENTIALS_REF}, status active, watching from now"
    )
    return mailbox_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the Gmail test account (task 6.10).")
    parser.add_argument("--address", required=True, help="the Gmail test account's address")
    parser.add_argument(
        "--org-id", type=UUID, default=DEMO_ORG_ID, help="tenant to register into (demo: Acme)"
    )
    args = parser.parse_args(argv)
    if not args.address.strip():
        print("FAIL usage: make connect-gmail ADDRESS=<test account address>", file=sys.stderr)
        return 2
    try:
        mailbox_id = asyncio.run(run(args.address, args.org_id))
    except ConnectError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"CONNECT GMAIL OK mailbox_id={mailbox_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
