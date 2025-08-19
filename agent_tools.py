import json
from typing import Any, Dict
from sqlalchemy.orm import Session
from services_catalog import search_products, get_product
from services_cart import load_cart, save_cart, cart_add, cart_remove, cart_str

TOOLS = [
  {"type":"function","function":{"name":"search_products","description":"Busca productos.",
    "parameters":{"type":"object","properties":{"query":{"type":"string"},"filters":{"type":"object","properties":{
      "category":{"type":"string"},"color":{"type":"string"},"size":{"type":"string"},
      "sleeve":{"type":"string","enum":["corta","larga"]},"use":{"type":"string"}}}},"required":["query"]}}},
  {"type":"function","function":{"name":"get_product","description":"Detalles por sku/url.",
    "parameters":{"type":"object","properties":{"product_ref":{"type":"string"}},"required":["product_ref"]}}},
  {"type":"function","function":{"name":"add_to_cart","description":"Agrega al carrito.",
    "parameters":{"type":"object","properties":{"sku":{"type":"string"},"name":{"type":"string"},"price":{"type":"number"},
      "size":{"type":["string","null"]},"color":{"type":["string","null"]},"qty":{"type":"integer","default":1}},
      "required":["sku","name","price"]}}},
  {"type":"function","function":{"name":"remove_from_cart","description":"Quita del carrito.",
    "parameters":{"type":"object","properties":{"sku":{"type":"string"},"size":{"type":["string","null"]}},"required":["sku"]}}},
  {"type":"function","function":{"name":"show_cart","description":"Devuelve carrito.","parameters":{"type":"object","properties":{}}}}
]

SYSTEM_PROMPT = (
    "Eres asesor de CASSANY. Llama a search_products para mostrar opciones; "
    "si el usuario quiere agregar, usa get_product si falta detalle y luego add_to_cart. "
    "Si falta talla para un producto con tallas, pregunta cuál. Responde breve y profesional."
)

def dispatch_tool(db: Session, session_id: str, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "search_products":
        return {"items": search_products(args["query"], args.get("filters") or {}, limite=6)}
    if name == "get_product":
        return {"item": get_product(args["product_ref"])}
    if name == "add_to_cart":
        cart = load_cart(db, session_id)
        cart = cart_add(cart, {"sku": args["sku"], "name": args["name"], "price": float(args["price"]),
                               "size": args.get("size"), "color": args.get("color"), "qty": int(args.get("qty",1))})
        save_cart(db, session_id, cart)
        return {"ok": True, "summary": cart_str(cart)}
    if name == "remove_from_cart":
        cart = load_cart(db, session_id); cart = cart_remove(cart, args["sku"], args.get("size"))
        save_cart(db, session_id, cart);   return {"ok": True, "summary": cart_str(cart)}
    if name == "show_cart":
        return {"summary": cart_str(load_cart(db, session_id))}
    return {"error": f"Unknown tool {name}"}
