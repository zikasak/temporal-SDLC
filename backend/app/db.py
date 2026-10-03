import psycopg
from psycopg.rows import dict_row

from app.config import APP_DATABASE_URL


async def connect() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(APP_DATABASE_URL, row_factory=dict_row)
