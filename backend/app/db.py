import psycopg
from psycopg.rows import DictRow, dict_row

from app.config import APP_DATABASE_URL


async def connect() -> psycopg.AsyncConnection[DictRow]:
    return await psycopg.AsyncConnection[DictRow].connect(APP_DATABASE_URL, row_factory=dict_row)
