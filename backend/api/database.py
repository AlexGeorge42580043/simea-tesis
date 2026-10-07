import psycopg2
from psycopg2.extras import RealDictCursor
from influxdb_client import InfluxDBClient
from contextlib import contextmanager

from config import (
    INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET,
    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DB
)

# -----------------------------
# POSTGRESQL
# -----------------------------

@contextmanager
def get_pg_connection():
    """
    Context manager para conexiones a PostgreSQL.
    Garantiza que la conexión se cierre correctamente incluso ante errores.
    """
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    try:
        yield conn
    finally:
        conn.close()

@contextmanager
def get_pg_cursor(dict_cursor: bool = True):
    """
    Context manager para cursores de PostgreSQL.
    Si dict_cursor=True, los resultados vienen como diccionarios.
    Hace commit automático al finalizar y rollback ante errores.
    """
    with get_pg_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

# -----------------------------
# INFLUXDB
# -----------------------------

_influx_client = None

def get_influx_client() -> InfluxDBClient:
    """
    Devuelve una instancia única (singleton) del cliente de InfluxDB.
    Reutilizar la conexión es más eficiente que crear una nueva en cada llamada.
    """
    global _influx_client
    if _influx_client is None:
        _influx_client = InfluxDBClient(
            url=INFLUX_URL,
            token=INFLUX_TOKEN,
            org=INFLUX_ORG
        )
    return _influx_client

def cerrar_influx_client():
    """Cierra el cliente de InfluxDB al apagar la API."""
    global _influx_client
    if _influx_client is not None:
        _influx_client.close()
        _influx_client = None