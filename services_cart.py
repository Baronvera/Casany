import json
from typing import Any, Dict, List, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

def load_cart(db: Session, session_id: str) -> List[Dict[str, Any]]:
    row = db.execute(text("SELECT carrito_json FROM pedidos WHERE session_id=:sid"), {"sid": session_id}).fetchone()
    raw = row[0] if row and row[0] else "[]"
    try:
        data = json.loads(raw);  return data if isinstance(data, list) else []
    except Exception:
        return []

def save_cart(db: Session, session_id: str, cart: List[Dict[str, Any]]):
    db.execute(text("UPDATE pedidos SET carrito_json=:j WHERE session_id=:sid"),
               {"j": json.dumps(cart, ensure_ascii=False), "sid": session_id})
    db.commit()
    subtotal = cart_total(cart)
    db.execute(text("UPDATE pedidos SET subtotal=:st WHERE session_id=:sid"), {"st": subtotal, "sid": session_id})
    db.commit()

def cart_add(cart: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Dict[str, Any]]:
    sku, size, color = item["sku"], item.get("size"), item.get("color")
    for it in cart:
        if it["sku"] == sku and it.get("size")==size and it.get("color")==color:
            it["qty"] = int(it.get("qty",1)) + int(item.get("qty",1));  return cart
    cart.append({"sku": sku, "name": item.get("name","Producto"), "price": float(item.get("price",0.0)),
                 "size": size, "color": color, "qty": int(item.get("qty",1))})
    return cart

def cart_remove(cart: List[Dict[str, Any]], sku: str, size: Optional[str]=None) -> List[Dict[str, Any]]:
    return [it for it in cart if not (it["sku"]==sku and it.get("size")==size)]

def cart_total(cart: List[Dict[str, Any]]) -> float:
    return sum(float(it.get("price",0.0))*int(it.get("qty",1)) for it in cart)

def cart_str(cart: List[Dict[str, Any]]) -> str:
    if not cart: return "Tu carrito está vacío."
    lines, total = [], 0
    for i, it in enumerate(cart, 1):
        price = int(it.get("price",0.0)); qty = int(it.get("qty",1)); total += price*qty
        tail = " ".join([x for x in [it.get("color"), it.get("size")] if x])
        lines.append(f"{i}. {it['name']} ({it['sku']}) {tail} x{qty} – ${price:,}")
    lines.append(f"\nTotal: ${total:,}")
    return "\n".join(lines)
