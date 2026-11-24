"""Application-wide settings read afresh from PostgreSQL for each model run."""
import logging
import os

from psycopg.types.json import Jsonb

from app.db import connect
from app.settings import endpoint as endpoint_storage
from app.settings.endpoint import OpenAIEndpoint
from app.settings.models import (
    AgentSettings, ModelSettings, WORKFLOW_LABELS, WorkflowSettings, WorkflowType, default_model_settings,
)

# Workflow types added after others, by the type whose stored settings they start with.
INITIAL_SOURCES: dict[str, str] = {'requirements_review': 'requirements'}
# Marks a save that leaves the OpenAI endpoint as it is.
UNCHANGED = object()
# Environment settings that moved to the Settings page, with where they are set now.
MOVED_TO_SETTINGS = {
    'AGENT_CONTEXT_TOKENS': 'the compaction threshold of each workflow type',
    'CONTRACT_EMBED_CHARS': 'the contract embedding limit',
}
logger = logging.getLogger(__name__)


async def ensure_schema() -> None:
    for name, setting in MOVED_TO_SETTINGS.items():
        if os.getenv(name):
            logger.warning('%s is no longer read; set %s on the Settings page instead',
                           name, setting)
    async with await connect() as conn:
        await conn.execute('CREATE SCHEMA IF NOT EXISTS app')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS app.workflow_settings (
                workflow_type text PRIMARY KEY,
                settings jsonb NOT NULL
            )
        ''')
        # A single row: the settings shared by the agents of every workflow type.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS app.agent_settings (
                id boolean PRIMARY KEY DEFAULT true CHECK (id),
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


async def get_agent_settings() -> AgentSettings:
    """The saved agent settings, or their defaults while none are saved."""
    async with await connect() as conn:
        row = await (await conn.execute(
            'SELECT settings FROM app.agent_settings')).fetchone()
    return AgentSettings.model_validate(row['settings']) if row else AgentSettings()


async def save_settings(settings: WorkflowSettings, endpoint: object = UNCHANGED,
                        encrypted_api_key: bytes | None = None,
                        agent: AgentSettings | None = None) -> WorkflowSettings:
    """Save the workflow settings, the agent settings unless `agent` is None and, unless
    `endpoint` is UNCHANGED, replace (an OpenAIEndpoint) or remove (None) the OpenAI
    endpoint, all in one transaction: a failed save never leaves a partially updated page."""
    async with await connect() as conn:
        async with conn.transaction():
            if agent is not None:
                await conn.execute('''
                    INSERT INTO app.agent_settings (id, settings) VALUES (true, %s)
                    ON CONFLICT (id) DO UPDATE SET settings = EXCLUDED.settings
                ''', (Jsonb(agent.model_dump()),))
            for workflow_type, model in sorted(settings.workflows.items()):
                await conn.execute('''
                    INSERT INTO app.workflow_settings (workflow_type, settings) VALUES (%s, %s)
                    ON CONFLICT (workflow_type) DO UPDATE SET settings = EXCLUDED.settings
                ''', (workflow_type, Jsonb(model.model_dump())))
            if endpoint is not UNCHANGED:
                assert endpoint is None or isinstance(endpoint, OpenAIEndpoint)
                await endpoint_storage.save_endpoint(conn, endpoint, encrypted_api_key)
    return settings
