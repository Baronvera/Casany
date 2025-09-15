# carrito.py
from typing import List, Optional

def fmt_cop(v: float) -> str:
    try:
        return f"${float(v):,.0f}".replace(",", ".")
    except Exception:
        return "$0"

def cart_total(carrito: list) -> float:
    return sum(float(i.get("precio_unitario", 0.0)) * int(i.get("cantidad", 1)) for i in carrito)

def cart_summary_lines(carrito: list) -> List[str]:
    if not carrito:
        return ["Tu carrito está vacío."]
    lines = []
    for i, it in enumerate(carrito, 1):
        precio = fmt_cop(it.get('precio_unitario', 0))
        qty = int(it.get("cantidad", 1))
        tail = " ".join([x for x in [(it.get("color") or ""), (it.get("talla") or "")] if x]).strip()
        tail = f" {tail}" if tail else ""
        lines.append(f"{i}. {it['nombre']} ({it['sku']}){tail} x{qty} – {precio} c/u")
    lines.append(f"\nTotal: {fmt_cop(cart_total(carrito))}")
    return lines

def item_exists(carrito: list, sku: str, talla: str = None, color: str = None) -> bool:
    return any(i for i in carrito if i["sku"] == sku and i.get("talla") == talla and i.get("color") == color)

def cart_add(carrito: list, sku: str, nombre: str, categoria: str,
             talla: str = None, color: str = None, cantidad: int = 1,
             precio_unitario: float = 0.0):
    for item in carrito:
        if item["sku"] == sku and item.get("talla") == talla and item.get("color") == color:
            item["cantidad"] = max(1, int(item.get("cantidad", 1))) + max(1, int(cantidad))
            return carrito
    carrito.append({
        "sku": sku, "nombre": nombre, "categoria": categoria,
        "talla": talla, "color": color, "cantidad": max(1, int(cantidad)),
        "precio_unitario": float(precio_unitario)
    })
    return carrito

def cart_update_qty(carrito: list, sku: str, talla: str = None, color: str = None, cantidad: int = 1):
    for item in carrito:
        if item["sku"] == sku and item.get("talla") == talla and item.get("color") == color:
            item["cantidad"] = max(1, int(cantidad))
            return carrito
    return carrito

def cart_remove(carrito: list, sku: str, talla: str = None, color: str = None):
    return [i for i in carrito if not (i["sku"] == sku and i.get("talla") == talla and i.get("color") == color)]

# Persistencia (las funciones siguientes dependen de tu tabla "pedidos")
from sqlalchemy.orm import Session
from sqlalchemy import text as sa_text

def carrito_load(pedido) -> list:
    try:
        sid = pedido.session_id
        db = Session.object_session(pedido) or None
        if db is None:
            # Fallback: abre una nueva sesión si lo prefieres
            from database import SessionLocal
            db = SessionLocal()
            close_after = True
        else:
            close_after = False
        row = db.execute(sa_text("SELECT carrito_json FROM pedidos WHERE session_id=:sid"), {"sid": sid}).fetchone()
        if close_after:
            db.close()
        import json
        raw = row[0] if row and row[0] else "[]"
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []

def carrito_save(db: Session, session_id: str, carrito: list):
    import json
    try:
        db.execute(sa_text("UPDATE pedidos SET carrito_json=:j WHERE session_id=:sid"),
                   {"j": json.dumps(carrito, ensure_ascii=False), "sid": session_id})
        db.commit()
    except Exception:
        db.rollback()
