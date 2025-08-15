# database.py — v2.2
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from models import Base

# Usa env si existe; por defecto sqlite en el directorio del proyecto.
# Ejemplo env:
#   DATABASE_URL=sqlite:///./pedidos.db
#   DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pedidos.db").strip()

ECHO_SQL = os.getenv("SQLALCHEMY_ECHO", "0") == "1"

is_sqlite = DATABASE_URL.startswith("sqlite")

# Para SQLite en archivo: check_same_thread=False es necesario con FastAPI
connect_args = {"check_same_thread": False} if is_sqlite else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    # ping al pool para reciclar conexiones caídas
    pool_pre_ping=True,
    # En SQLite no recomendamos pools agresivos; el default está bien.
    # (Si usas otro motor, puedes configurar pool_size, max_overflow, etc.)
    echo=ECHO_SQL,
    future=True,
)

# PRAGMAs para mejorar concurrencia y consistencia en SQLite
if is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, connection_record):  # type: ignore
        cur = dbapi_conn.cursor()
        # Evita llaves foráneas huérfanas si las agregas en el futuro
        cur.execute("PRAGMA foreign_keys=ON;")
        # Mejor rendimiento en concurrencia con Uvicorn/Starlette
        cur.execute("PRAGMA journal_mode=WAL;")
        # Balance seguridad/rendimiento
        cur.execute("PRAGMA synchronous=NORMAL;")
        # Espera hasta 30s si el archivo está bloqueado (otra transacción)
        cur.execute("PRAGMA busy_timeout=30000;")
        cur.close()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # mantiene objetos “vivos” tras commit
    future=True,
)

def init_db() -> None:
    """Crea tablas si no existen."""
    Base.metadata.create_all(bind=engine)

