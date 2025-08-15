# crud.py — v2.3
from __future__ import annotations
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select
from models import Pedido
from datetime import datetime, timezone
import random

# Campos que permitimos tocar desde la app
ALLOWED_FIELDS = {
    "producto", "cantidad", "talla", "precio_unitario", "subtotal",
    "nombre_cliente", "telefono", "direccion", "ciudad",
    "metodo_pago", "metodo_entrega", "punto_venta",
    "notas", "estado", "last_activity", "sugeridos",
    "datos_personales_advertidos", "saludo_enviado", "last_msg_id",
    "numero_confirmacion"  # rara vez, pero lo dejamos por compatibilidad
}

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _safe_int(v, default=0) -> int:
    try:
        i = int(v)
        return i if i >= 0 else default
    except Exception:
        return default

def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if f >= 0 else default
    except Exception:
        return default

# --- en crud.py ---
def _safe_str(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip()
    # para datetime, int, float, etc. devolver tal cual
    return v


def _calc_subtotal(cantidad, precio_u) -> float:
    try:
        return max(0.0, float(cantidad) * float(precio_u))
    except Exception:
        return 0.0

def _genera_numero_confirmacion() -> str:
    fecha = datetime.utcnow().strftime("%Y%m%d")
    aleatorio = f"{random.randint(100, 999)}"
    return f"CAS-{fecha}-{aleatorio}"

def _numero_confirmacion_unico(db: Session) -> str:
    # Intenta hasta 5 veces evitar colisión (muy improbable)
    for _ in range(5):
        numero = _genera_numero_confirmacion()
        exist = db.execute(
            select(Pedido.id).where(Pedido.numero_confirmacion == numero)
        ).first()
        if not exist:
            return numero
    # Último recurso: añade sufijo largo
    return f"{_genera_numero_confirmacion()}-{random.randint(1000, 9999)}"

# ------------------ CRUD ------------------

def crear_pedido(db: Session, datos: Dict[str, Any]) -> Pedido:
    """
    Crea un pedido con defaults consistentes con el modelo.
    Rellena campos nuevos para no romper flujos posteriores.
    """
    ahora = _now_utc()
    numero = datos.get("numero_confirmacion") or _numero_confirmacion_unico(db)

    cantidad = _safe_int(datos.get("cantidad", 0))
    precio_u = _safe_float(datos.get("precio_unitario", 0.0))
    subtotal = _safe_float(datos.get("subtotal", _calc_subtotal(cantidad, precio_u)))

    pedido = Pedido(
        session_id=_safe_str(datos.get("session_id")),
        # producto
        producto=_safe_str(datos.get("producto")),
        cantidad=cantidad,
        talla=_safe_str(datos.get("talla")),
        precio_unitario=precio_u,
        subtotal=subtotal,
        # cliente/entrega
        nombre_cliente=_safe_str(datos.get("nombre_cliente")),
        telefono=_safe_str(datos.get("telefono")),
        direccion=_safe_str(datos.get("direccion")),
        ciudad=_safe_str(datos.get("ciudad")),
        metodo_pago=_safe_str(datos.get("metodo_pago")),
        metodo_entrega=_safe_str(datos.get("metodo_entrega")),
        punto_venta=_safe_str(datos.get("punto_venta")),
        # meta
        notas=_safe_str(datos.get("notas")),
        numero_confirmacion=numero,
        estado=_safe_str(datos.get("estado") or "pendiente") or "pendiente",
        # tiempos
        last_activity=datos.get("last_activity") or ahora,
        # auxiliares
        sugeridos=_safe_str(datos.get("sugeridos")),
        datos_personales_advertidos=_safe_int(datos.get("datos_personales_advertidos", 0)),
        saludo_enviado=_safe_int(datos.get("saludo_enviado", 0)),
        last_msg_id=_safe_str(datos.get("last_msg_id")),
    )
    db.add(pedido)
    db.commit()
    db.refresh(pedido)
    return pedido

def obtener_pedido_por_sesion(db: Session, session_id: str) -> Optional[Pedido]:
    return db.query(Pedido).filter(Pedido.session_id == session_id).first()

def actualizar_pedido_por_sesion(db: Session, session_id: str, campo: str, valor) -> Optional[Pedido]:
    """
    Actualiza un único campo permitido. Recalcula subtotal si corresponde.
    Auto-actualiza last_activity (salvo cuando tú mismo lo estás seteando).
    """
    pedido = obtener_pedido_por_sesion(db, session_id)
    if not pedido:
        return None

    if campo not in ALLOWED_FIELDS:
        # ignorar silenciosamente para no romper el flujo
        return pedido

    # Normalizaciones por tipo
    if campo == "cantidad":
        valor = _safe_int(valor)
    elif campo in ("precio_unitario", "subtotal"):
        valor = _safe_float(valor)
    elif campo in ("datos_personales_advertidos", "saludo_enviado"):
        valor = 1 if str(valor) in ("1", "true", "True") or valor is True else 0
    else:
        valor = _safe_str(valor)

    setattr(pedido, campo, valor)

    # Recalcula subtotal si cambia cantidad o precio_unitario (y no nos pasaron subtotal explícito)
    if campo in ("cantidad", "precio_unitario"):
        if not _safe_float(getattr(pedido, "subtotal", 0), None):
            pedido.subtotal = _calc_subtotal(pedido.cantidad, pedido.precio_unitario)

    # Auto-bump de last_activity, exceptuando cuando el propio campo es last_activity
    if campo != "last_activity":
        pedido.last_activity = _now_utc()

    db.commit()
    db.refresh(pedido)
    return pedido

def actualizar_pedido_por_sesion_many(db: Session, session_id: str, updates: Dict[str, Any]) -> Optional[Pedido]:
    """
    Actualiza múltiples campos en una sola transacción. Útil para pasos del flujo.
    """
    pedido = obtener_pedido_por_sesion(db, session_id)
    if not pedido:
        return None

    touched = False
    for campo, valor in (updates or {}).items():
        if campo not in ALLOWED_FIELDS:
            continue
        touched = True
        if campo == "cantidad":
            valor = _safe_int(valor)
        elif campo in ("precio_unitario", "subtotal"):
            valor = _safe_float(valor)
        elif campo in ("datos_personales_advertidos", "saludo_enviado"):
            valor = 1 if str(valor) in ("1", "true", "True") or valor is True else 0
        else:
            valor = _safe_str(valor)
        setattr(pedido, campo, valor)

    if touched:
        # Recalcula subtotal si falta o es cero y tenemos base suficiente
        if (not pedido.subtotal or pedido.subtotal <= 0) and (pedido.cantidad and pedido.precio_unitario):
            pedido.subtotal = _calc_subtotal(pedido.cantidad, pedido.precio_unitario)

        pedido.last_activity = _now_utc()
        db.commit()
        db.refresh(pedido)

    return pedido
