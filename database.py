# database.py — Cloud Run ready (v3.0)
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from models import Base

# ======== Detección de entorno ========
IN_CLOUD_RUN = "K_SERVICE" in os.environ or "K_REVISION" in os.environ

# ======== Configuración común ========
ECHO_SQL = os.getenv("SQLALCHEMY_ECHO", "0") == "1"

# Pool recomendado para serverless (Cloud Run)
POOL_SIZE = int(os.getenv("SQLALCHEMY_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("SQLALCHEMY_MAX_OVERFLOW", "2"))
POOL_TIMEOUT = int(os.getenv("SQLALCHEMY_POOL_TIMEOUT", "30"))  # segundos
POOL_RECYCLE = int(os.getenv("SQLALCHEMY_POOL_RECYCLE", "1800"))  # segundos

# ======== Modo 1: DATABASE_URL explícito ========
# Ejemplos:
#   postgresql+psycopg://user:pass@host:5432/dbname
#   postgresql+pg8000://user:pass@host:5432/dbname
#   mysql+pymysql://user:pass@host:3306/dbname
#   sqlite:///./pedidos.db
default_sqlite_path = os.getenv("DB_PATH", "/tmp/pedidos.db" if IN_CLOUD_RUN else "./pedidos.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{default_sqlite_path}").strip()

# ======== Modo 2: Cloud SQL Connector (Postgres) ========
# Si NO hay DATABASE_URL pero SÍ hay CLOUD_SQL_INSTANCE, armamos el engine con creator=getconn
USE_CLOUD_SQL_CONNECTOR = (
    not os.getenv("DATABASE_URL")
    and bool(os.getenv("CLOUD_SQL_INSTANCE"))
    and bool(os.getenv("DB_USER"))
    and bool(os.getenv("DB_PASS"))
    and bool(os.getenv("DB_NAME"))
)

engine = None

if USE_CLOUD_SQL_CONNECTOR:
    # Requiere: pip install google-cloud-sql-connector[pg8000] pg8000
    try:
        from google.cloud.sql.connector import Connector, IPTypes  # type: ignore
        import pg8000  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "Faltan dependencias para Cloud SQL Connector. "
            "Instala: google-cloud-sql-connector[pg8000] y pg8000"
        ) from exc

    INSTANCE = os.getenv("CLOUD_SQL_INSTANCE")  # e.g. project:region:instance
    DB_USER = os.getenv("DB_USER")
    DB_PASS = os.getenv("DB_PASS")
    DB_NAME = os.getenv("DB_NAME")
    IP_TYPE = (os.getenv("DB_IP_TYPE") or "PUBLIC").upper()
    ip_mode = IPTypes.PRIVATE if IP_TYPE == "PRIVATE" else IPTypes.PUBLIC

    connector = Connector()  # se cerrará al final del proceso

    def getconn():
        # Crea una conexión nueva por solicitud del pool
        return connector.connect(
            INSTANCE,
            "pg8000",
            user=DB_USER,
            password=DB_PASS,
            db=DB_NAME,
            ip_type=ip_mode,
        )

    engine = create_engine(
        "postgresql+pg8000://",
        creator=getconn,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_recycle=POOL_RECYCLE,
        echo=ECHO_SQL,
        future=True,
    )

else:
    # ======== Modo 1 (DATABASE_URL) o 3 (SQLite fallback) ========
    is_sqlite = DATABASE_URL.startswith("sqlite")

    connect_args = {"check_same_thread": False} if is_sqlite else {}

    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        pool_pre_ping=True,
        # Usa un pool pequeño si NO es SQLite.
        **(
            {} if is_sqlite else dict(
                pool_size=POOL_SIZE,
                max_overflow=MAX_OVERFLOW,
                pool_timeout=POOL_TIMEOUT,
                pool_recycle=POOL_RECYCLE,
            )
        ),
        echo=ECHO_SQL,
        future=True,
    )

    # PRAGMAs para mejorar concurrencia/consistencia en SQLite
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, connection_record):  # type: ignore
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.close()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)

def init_db() -> None:
    """Crea tablas si no existen."""
    Base.metadata.create_all(bind=engine)

