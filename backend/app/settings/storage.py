"""Application-wide settings read afresh from PostgreSQL for each model run."""
from psycopg.types.json import Jsonb

from app.db import connect
from app.settings.models import ModelSettings, WORKFLOW_LABELS, WorkflowSettings, WorkflowType

# Workflow types added after others, by the type whose stored settings they start with.
INITIAL_SOURCES: dict[str, str] = {'requirements_review': 'requirements'}


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute('CREATE SCHEMA IF NOT EXISTS app')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS app.workflow_settings (
                workflow_type text PRIMARY KEY,
                settings jsonb NOT NULL
            )
        ''')
        for workflow_type in WORKFLOW_LABELS:
            source = INITIAL_SOURCES.get(workflow_type)
            if source:
                # A type added later starts with the model of the one it splits from.
                await conn.execute('''
                    INSERT INTO app.workflow_settings (workflow_type, settings)
                    SELECT %s, settings FROM app.workflow_settings WHERE workflow_type = %s
                    ON CONFLICT (workflow_type) DO NOTHING
                ''', (workflow_type, source))
            await conn.execute('''
                INSERT INTO app.workflow_settings (workflow_type, settings) VALUES (%s, %s)
                ON CONFLICT (workflow_type) DO NOTHING
            ''', (workflow_type, Jsonb(ModelSettings().model_dump())))


async def get_settings() -> WorkflowSettings:
    async with await connect() as conn:
        rows = await (await conn.execute(
            'SELECT workflow_type, settings FROM app.workflow_settings')).fetchall()
    return WorkflowSettings(workflows={row['workflow_type']: row['settings'] for row in rows})


async def get_workflow_settings(workflow_type: WorkflowType) -> ModelSettings:
    async with await connect() as conn:
        row = await (await conn.execute(
            'SELECT settings FROM app.workflow_settings WHERE workflow_type = %s',
            (workflow_type,))).fetchone()
    if row is None:
        raise ValueError(f'Missing settings for workflow: {workflow_type}')
    return ModelSettings.model_validate(row['settings'])


async def save_settings(settings: WorkflowSettings) -> WorkflowSettings:
    # One transaction: a failed save never leaves a partially updated page.
    async with await connect() as conn:
        for workflow_type, model in sorted(settings.workflows.items()):
            await conn.execute('''
                INSERT INTO app.workflow_settings (workflow_type, settings) VALUES (%s, %s)
                ON CONFLICT (workflow_type) DO UPDATE SET settings = EXCLUDED.settings
            ''', (workflow_type, Jsonb(model.model_dump())))
    return settings
