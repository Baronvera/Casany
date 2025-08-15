# models.py
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text, func,
    CheckConstraint, Index
)
from sqlalchemy.orm import declarative_base  # SQLAlchemy 2.x

Base = declarative_base()

class Pedido(Base):
    __tablename__ = "pedidos"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), index=True, nullable=False)

    # Datos del producto
    producto = Column(String(255), nullable=True)
    cantidad = Column(Integer, nullable=False, default=0, server_default="0")
    talla = Column(String(32), nullable=True)
    precio_unitario = Column(Float, nullable=False, default=0.0, server_default="0")
    subtotal = Column(Float, nullable=False, default=0.0, server_default="0")

    # Datos del cliente / entrega
    nombre_cliente = Column(String(255), nullable=True)
    telefono = Column(String(64), nullable=True)
    direccion = Column(String(255), nullable=True)
    ciudad = Column(String(128), nullable=True)
    metodo_pago = Column(String(64), nullable=True)
    metodo_entrega = Column(String(32), nullable=True)
    punto_venta = Column(String(128), nullable=True)

    # Meta del pedido
    notas = Column(Text, nullable=True)  # aquí guardamos también [CAT=...] si aplica
    numero_confirmacion = Column(String(64), unique=True, nullable=True)
    estado = Column(String(32), nullable=False, default="pendiente", server_default="pendiente")

    # Tiempos (timezone-aware). En SQLite funcionará con UTC por defecto.
    fecha_creacion = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_activity  = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    # Auxiliares para conversación / sugerencias
    sugeridos = Column(Text, nullable=True)  # guarda URLs sugeridas (espacio-separadas)
    datos_personales_advertidos = Column(Integer, nullable=False, default=0, server_default="0")  # 0/1
    saludo_enviado = Column(Integer, nullable=False, default=0, server_default="0")  # 0/1
    last_msg_id = Column(String(128), nullable=True)  # último wamid procesado

    __table_args__ = (
        # Evita valores negativos que rompen el flujo
        CheckConstraint("cantidad >= 0", name="ck_pedidos_cantidad_nonneg"),
        CheckConstraint("precio_unitario >= 0", name="ck_pedidos_precio_nonneg"),
        CheckConstraint("subtotal >= 0", name="ck_pedidos_subtotal_nonneg"),

        # Índices extra para consultas comunes
        Index("ix_pedidos_estado", "estado"),
        Index("ix_pedidos_last_msg_id", "last_msg_id"),
    )

    def __repr__(self) -> str:
        return f"<Pedido id={self.id} sesion={self.session_id} estado={self.estado}>"
