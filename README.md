# hotel-whatsapp-sla

communicator

## Local development

```bash
docker compose up -d
alembic upgrade head
uvicorn app.main:app --reload --port 8000
celery -A app.tasks worker --loglevel=INFO
celery -A app.tasks beat --loglevel=INFO
ngrok http 8000
pytest
```

## Deploying a migration

`alembic upgrade head` migrates only the database in `DATABASE_URL`. In
multi-tenant mode each hotel has **its own database**, and those are migrated
only when the tenant is first provisioned. A deploy that ships a migration and
skips the tenant step leaves older hotels on the old schema, where the ORM
starts failing on every query — an outage for those hotels, not a degradation.

So the order on every deploy is:

```bash
alembic upgrade head
```

```bash
python -m app.scripts.migrate_tenants
```

To verify without changing anything (exits non-zero if any tenant is behind,
so it works as a deploy gate or CI check):

```bash
python -m app.scripts.migrate_tenants --check
```

The app also logs a `tenant_schema_drift` error at startup for any tenant that
is behind, so a missed step is visible in the logs rather than only in failing
webhooks.
