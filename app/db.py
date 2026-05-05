import socket
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector

logger = logging.getLogger(__name__)

_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None


def _ipv4_connect_params(dsn: str) -> dict:
    params = psycopg2.extensions.parse_dsn(dsn)
    hostname = params.get("host", "")
    if hostname:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
            params["hostaddr"] = infos[0][4][0]
            print(f"Resolved {hostname} → {params['hostaddr']} (IPv4)", flush=True)
        except Exception as e:
            print(f"IPv4 resolution failed for {hostname}: {e}", flush=True)
    return params


@contextmanager
def _db():
    conn = _pool.getconn()
    register_vector(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
