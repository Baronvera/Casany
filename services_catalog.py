import hashlib
from typing import Any, Dict, List, Optional
from woocommerce_gpt_utils import sugerir_productos, detectar_categoria

def _sku_from(url: Optional[str], name: str) -> str:
    base = (url or name or "SKU").encode("utf-8")
    return "SKU-" + hashlib.sha1(base).hexdigest()[:10].upper()

def _normalize(p: Dict[str, Any]) -> Dict[str, Any]:
    name = p.get("nombre") or p.get("name") or "Producto"
    url  = p.get("url")
    price = float(p.get("precio") or p.get("price") or 0.0)
    sizes = p.get("tallas_disponibles") or p.get("sizes") or []
    return {"sku": _sku_from(url, name), "name": name, "url": url, "price": price, "sizes": sizes}

def search_products(query: str, filtros: Optional[Dict[str, Any]]=None, limite: int=6) -> List[Dict[str, Any]]:
    filtros = filtros or {}
    res = sugerir_productos(query, limite=limite) or {}
    prods = res.get("productos") or []
    if not prods:
        cat, _ = detectar_categoria(query)
        if cat:
            prods = (sugerir_productos(cat, limite=limite) or {}).get("productos") or []
    norm = [_normalize(p) for p in prods]
    want_size = (filtros.get("size") or "").upper()
    want_color = (filtros.get("color") or "").lower()
    out = []
    for p in norm:
        if want_size and p["sizes"] and want_size not in [s.upper() for s in p["sizes"]]:
            continue
        if want_color and want_color not in p["name"].lower():
            continue
        out.append(p)
    return out[:limite]

def get_product(product_ref: str) -> Dict[str, Any]:
    return {"sku": product_ref, "name": "Producto", "price": 100000.0, "url": None, "sizes": ["S","M","L","XL"]}
