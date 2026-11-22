"""Application-wide settings read afresh from PostgreSQL for each model run."""
from psycopg.types.json import Jsonb

from app.db import connect
from app.settings import endpoint as endpoint_storage
from app.settings.endpoint import OpenAIEndpoint
from app.settings.models import (
    ModelSettings, WORKFLOW_LABELS, WorkflowSettings, WorkflowType, default_model_settings,
)

# Workflow types added after others, by the type whose stored settings they start with.
INITIAL_SOURCES: dict[str, str] = {'requirements_review': 'requirements'}
# Marks a save that leaves the OpenAI endpoint as it is.
UNCHANGED = object()


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute('CREATE SCHEMA IF NOT EXISTS app')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS app.workflow_settings (
                workflow_type text PRIMARY KEY,
                settings jsonb NOT NULL
            )
        ''')
        await endpoint_storage.ensure_schema(conn)
        for workflow_type, source in INITIAL_SOURCES.items():
            # A type added later starts with the model of the one it splits from. A type
            # with nothing saved gets its default when read (default_model_settings).
            await conn.execute('''
                INSERT INTO app.workflow_settings (workflow_type, settings)
                SELECT %s, settings FROM app.workflow_settings WHERE workflow_type = %s
                ON CONFLICT (workflow_type) DO NOTHING
            ''', (workflow_type, source))


async def _openai_ready(conn) -> bool:
    row = await (await conn.execute('SELECT 1 FROM app.openai_endpoint')).fetchone()
    return row is not None


async def get_settings() -> WorkflowSettings:
    async with await connect() as conn:
        rows = await (await conn.execute(
            'SELECT workflow_type, settings FROM app.workflow_settings')).fetchall()
        saved = {row['workflow_type']: row['settings'] for row in rows
                 if row['workflow_type'] in WORKFLOW_LABELS}
        default = None
        if set(saved) != set(WORKFLOW_LABELS):
            default = default_model_settings(await _openai_ready(conn)).model_dump()
    return WorkflowSettings(workflows={
        workflow_type: saved.get(workflow_type, default) for workflow_type in WORKFLOW_LABELS})


async def get_workflow_settings(workflow_type: WorkflowType) -> ModelSettings:
    async with await connect() as conn:
        row = await (await conn.execute(
            'SELECT settings FROM app.workflow_settings WHERE workflow_type = %s',
            (workflow_type,))).fetchone()
        if row is None:
            if workflow_type not in WORKFLOW_LABELS:
                raise ValueError(f'Missing settings for workflow: {workflow_type}')
            return default_model_settings(await _openai_ready(conn))
    return ModelSettings.model_validate(row['settings'])


async def save_settings(settings: WorkflowSettings, endpoint: object = UNCHANGED,
                        encrypted_api_key: bytes | None = None) -> WorkflowSettings:
    """Save the workflow settings and, unless `endpoint` is UNCHANGED, replace (an
    OpenAIEndpoint) or remove (None) the OpenAI endpoint, all in one transaction: a failed
    save never leaves a partially updated page."""
    async with await connect() as conn:
        async with conn.transaction():
            for workflow_type, model in sorted(settings.workflows.items()):
                await conn.execute('''
                    INSERT INTO app.workflow_settings (workflow_type, settings) VALUES (%s, %s)
                    ON CONFLICT (workflow_type) DO UPDATE SET settings = EXCLUDED.settings
                ''', (workflow_type, Jsonb(model.model_dump())))
            if endpoint is not UNCHANGED:
                assert endpoint is None or isinstance(endpoint, OpenAIEndpoint)
                await endpoint_storage.save_endpoint(conn, endpoint, encrypted_api_key)
    return settings
