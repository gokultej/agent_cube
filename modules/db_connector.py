import os
import psycopg2
from psycopg2 import pool
from dotenv import load_dotenv
import logging

load_dotenv()

class DBConnector:
    _pool = None

    @classmethod
    def get_pool(cls):
        if cls._pool is None:
            cls._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=5,
                host=os.getenv("DB_HOST", "localhost"),
                port=int(os.getenv("DB_PORT", 5432)),
                dbname=os.getenv("DB_NAME"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                options=f"-c search_path={os.getenv('DB_SCHEMA', 'public')}"
            )
        return cls._pool

    @classmethod
    def execute_query(cls, sql, params=None):
        """Execute a SQL query and return results as list of dicts."""
        conn = cls.get_pool().getconn()
        try:
            with conn.cursor() as cur:
                # Pass params only when supplied; forcing {} can break positional/no-param SQL.
                if not params:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                return [dict(zip(columns, row)) for row in rows]
        finally:
            cls.get_pool().putconn(conn)

    @classmethod
    def execute_scalar(cls, sql, params=None):
        """Execute a query returning a single value."""
        results = cls.execute_query(sql, params)
        if results:
            return list(results[0].values())[0]
        return None

    @classmethod
    def test_connection(cls):
        try:
            result = cls.execute_scalar("SELECT NOW()")
            logging.info(f"DB connected — server time: {result}")
            return True
        except Exception as e:
            logging.error(f"DB connection failed: {e}")
            return False
