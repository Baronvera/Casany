# migrar_schema_pedidos.py
from sqlalchemy import create_engine, text
from contextlib import contextmanager

DB_URL = "sqlite:///./pedidos.db"  # ruta consistente con tu proyecto

engine = create_engine(DB_URL, connect_args={"check_same_thread": False}, future=True)

@contextmanager
def begin():
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
            trans.commit()
        except:
            trans.rollback()
            raise

def has_column(conn, table: str, col: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM pragma_table_info(:t) WHERE name=:c"),
        {"t": table, "c": col},
    ).fetchone()
    return bool(row)

def add_column_if_missing(conn, table: str, ddl: str, col: str):
    if not has_column(conn, table, col):
        print(f"➕ Agregando columna {col} …")
        conn.execute(text(ddl))
    else:
        print(f"✓ {col} ya existe")

def create_index_if_missing(conn, idx_name: str, table: str, col: str):
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col})"))
    print(f"✓ Índice {idx_name} listo")

def main():
    with begin() as conn:
        # --- columnas que usa el agente ---
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN last_activity DATETIME DEFAULT (CURRENT_TIMESTAMP)",
            "last_activity"
        )
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN sugeridos TEXT",
            "sugeridos"
        )
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN punto_venta TEXT",
            "punto_venta"
        )
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN datos_personales_advertidos INTEGER DEFAULT 0",
            "datos_personales_advertidos"
        )
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN telefono TEXT",
            "telefono"
        )
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN saludo_enviado INTEGER DEFAULT 0",
            "saludo_enviado"
        )
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN last_msg_id TEXT",
            "last_msg_id"
        )
        # (por si aún no existen de tu schema actual)
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN precio_unitario FLOAT DEFAULT 0",
            "precio_unitario"
        )
        add_column_if_missing(
            conn, "pedidos",
            "ALTER TABLE pedidos ADD COLUMN subtotal FLOAT DEFAULT 0",
            "subtotal"
        )

        # --- inicialización de datos preexistentes ---
        # last_activity = fecha_creacion si está nulo
        conn.execute(text(
            """
            UPDATE pedidos
            SET last_activity = COALESCE(last_activity, fecha_creacion, CURRENT_TIMESTAMP)
            """
        ))
        # normaliza flags a 0/1
        conn.execute(text(
            """
            UPDATE pedidos
            SET datos_personales_advertidos = COALESCE(datos_personales_advertidos, 0),
                saludo_enviado = COALESCE(saludo_enviado, 0)
            """
        ))
        # asegura números no negativos
        conn.execute(text(
            """
            UPDATE pedidos
            SET cantidad = COALESCE(cantidad, 0),
                precio_unitario = COALESCE(precio_unitario, 0),
                subtotal = COALESCE(subtotal, 0)
            """
        ))

        # --- índices útiles para el agente ---
        create_index_if_missing(conn, "ix_pedidos_estado", "pedidos", "estado")
        create_index_if_missing(conn, "ix_pedidos_last_msg_id", "pedidos", "last_msg_id")

    print("✅ Migración completada.")

if __name__ == "__main__":
    main()
