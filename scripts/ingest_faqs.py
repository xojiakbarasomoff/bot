r"""Load a clinic's FAQ list from a JSON file into the knowledge_base table.

The JSON is an array of objects with "question", "answer", and an optional
"category" -- see data/faqs.json. Embedding each question costs an API call
to the configured provider, so this is a deliberate, run-it-yourself script
rather than anything that happens on startup.

Idempotent, because app.services.knowledge_base.ingest_faqs matches on the
exact question text: re-running after editing an answer updates that row in
place. Editing a *question* leaves the old row behind, since its text no
longer matches anything in the file -- delete it by hand if that matters.

Which tenant the rows belong to is resolved from the Instagram channel, the
same way the rest of the pipeline finds it. With exactly one channel in the
database that needs no argument; with several, name one via IG_ACCOUNT_ID.

Usage, from the repo root:

    python scripts/ingest_faqs.py data/faqs.json

On a managed host where the database is only reachable from inside the
cluster, run it inside a deployed container instead:

    railway ssh --service bot -- python scripts/ingest_faqs.py data/faqs.json
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import db_session
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.channel import Channel
from app.services.knowledge_base import FAQImport, ingest_faqs

_CHANNEL_TYPE = "instagram"


async def _resolve_tenant_id(session: AsyncSession, ig_account_id: str | None) -> uuid.UUID:
    """The tenant whose knowledge base these FAQs belong to.

    Resolved from the channel rather than taken as a raw UUID argument: a
    mistyped UUID would silently seed a tenant nobody serves, and the rows
    would then be invisible to every query the webhook makes.
    """
    query = select(Channel).where(Channel.type == _CHANNEL_TYPE)
    if ig_account_id is not None:
        query = query.where(Channel.external_id == ig_account_id)

    channels = list((await session.execute(query)).scalars())

    if not channels:
        which = f" with external_id={ig_account_id!r}" if ig_account_id else ""
        sys.exit(f"No {_CHANNEL_TYPE} channel found{which} - nothing to attach FAQs to.")
    if len(channels) > 1:
        ids = ", ".join(sorted(c.external_id for c in channels))
        sys.exit(
            f"{len(channels)} {_CHANNEL_TYPE} channels exist ({ids}). "
            "Set IG_ACCOUNT_ID to pick one."
        )
    return channels[0].tenant_id


async def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"Usage: {sys.argv[0]} <faqs.json>")

    path = Path(sys.argv[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"No such file: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")

    if not isinstance(raw, list):
        sys.exit(f"{path} must contain a JSON array of FAQ objects, got {type(raw).__name__}.")

    # Validate everything before the first embedding call, so a typo in the
    # last row doesn't surface after paying for 97 embeddings ahead of it.
    try:
        faqs = [FAQImport.model_validate(item) for item in raw]
    except Exception as exc:  # noqa: BLE001 - pydantic's message is the useful part
        sys.exit(f"Invalid FAQ entry in {path}: {exc}")

    if not faqs:
        print(f"{path} is empty - nothing to ingest.")
        return

    async with db_session() as session:
        tenant_id = await _resolve_tenant_id(session, os.environ.get("IG_ACCOUNT_ID"))
        token = set_current_tenant(tenant_id)
        try:
            rows = await ingest_faqs(session, faqs)
            await session.commit()
        finally:
            reset_current_tenant(token)

    print(f"Ingested {len(rows)} FAQ rows into tenant_id={tenant_id} from {path}.")


if __name__ == "__main__":
    asyncio.run(main())
