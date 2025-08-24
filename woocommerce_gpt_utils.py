"""woocommerce_gpt_utils.py  –  v2.3
Funciones auxiliares para:
• Detectar la intención de compra en lenguaje natural
• Consultar WooCommerce y devolver productos realmente disponibles
• Mostrar tallas disponibles por producto (variaciones)
• Filtrar por atributos declarados por el usuario (manga larga/corta, guayabera, color básico, lociones)
"""
from __future__ import annotations
import os
import re
import unicodedata
from typing import Dict, List, Tuple, Union, Optional

import requests
from dotenv import load_dotenv
from rapidfuzz import process, fuzz

#  Configuración
load_dotenv()

WC_API_URL       = os.getenv("WOOCOMMERCE_API_URL")
WC_CONSUMER_KEY  = os.getenv("WOOCOMMERCE_CONSUMER_KEY")
WC_CONSUMER_SEC  = os.getenv("WOOCOMMERCE_CONSUMER_SECRET")

# Mapa categoría → id (actualizado con todas las categorías que me diste)
CATEGORY_IDS: Dict[str, int] = {
    "accesorios": 238,
    "bermudas":   228,
    "blazers":    225,
    "camisas":    209,
    "guayaberas": 209,  # misma categoría que camisas, filtraremos por subtipo
    "camisetas":  229,
    "calzado":    216,
    "cinturon":   295,
    "jeans":      211,
    "pantalones": 212,
    "sueteres":   230,
    "trajes":     213,
}

# Sinónimos y expresiones habituales → categoría canonical
SYNONYMS: Dict[str, str] = {
    # --- Camisas ---
    "camisa": "camisas",
    "guayabera": "camisas",
    "guayaberas": "camisas",
    "manga corta": "camisas",
    "manga larga": "camisas",

    # --- Camisetas ---
    "tshirt": "camisetas",
    "t-shirt": "camisetas",
    "playera": "camisetas",

    # --- Jeans y pantalones ---
    "jean": "jeans",
    "denim": "jeans",
    "pantalón": "pantalones",
    "pantalon": "pantalones",

    # --- Suéteres ---
    "saco": "sueteres",
    "buzo": "sueteres",

    # --- Bermudas ---
    "short": "bermudas",

    # --- Calzado ---
    "zapatos": "calzado",
    "mocasines": "calzado",

    # --- Lociones / perfumes (nuevo) ---
    "locion": "accesorios",
    "lociones": "accesorios",
    "perfume": "accesorios",
    "perfumes": "accesorios",
    "fragancia": "accesorios",
    "fragancias": "accesorios",
}

def _normalize(txt: str) -> str:
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return txt.lower()

def _tokenize(txt: str) -> List[str]:
    return re.findall(r"[a-záéíóúñü]+", txt.lower())

def _woo_get(endpoint: str, params: Dict[str, Union[str, int]]) -> list:
    base = f"{WC_API_URL}{endpoint}"
    params.update({
        "consumer_key": WC_CONSUMER_KEY,
        "consumer_secret": WC_CONSUMER_SEC,
    })
    resp = requests.get(base, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def _filtrar_stock(items: list) -> list:
    return [
        p for p in items
        if str(p.get("stock_status", "instock")) == "instock"
           and float(p.get("price", 0)) > 0
    ]

# ---------- NUEVO: detección de atributos en texto ----------
_COLORES_BASICOS = {
    "negro", "blanco", "azul", "rojo", "verde", "gris", "beige",
    "marron", "marrón", "café", "cafe", "amarillo", "naranja", "morado", "lila"
}

def detectar_atributos(texto_usuario: str) -> Dict[str, Optional[str]]:
    """Extrae atributos clave del texto del usuario."""
    t = _normalize(texto_usuario)
    attrs: Dict[str, Optional[str]] = {"manga": None, "subtipo": None, "color": None}

    # manga
    if "manga larga" in t:
        attrs["manga"] = "larga"
    elif "manga corta" in t:
        attrs["manga"] = "corta"

    # subtipo
    if "guayabera" in t or "guayaberas" in t:
        attrs["subtipo"] = "guayabera"

    # color (básico)
    for c in _COLORES_BASICOS:
        if re.search(rf"\b{re.escape(c)}\b", t):
            attrs["color"] = c
            break

    return attrs

def _texto_de_producto(p: dict) -> str:
    """Concatena campos útiles del producto para hacer filtros por texto."""
    parts = [
        p.get("name", ""),
        " ".join([c.get("name","") for c in p.get("categories", []) or []]),
        " ".join([t.get("name","") for t in p.get("tags", []) or []]),
    ]
    for a in (p.get("attributes") or []):
        parts.append(a.get("name", ""))
        for opt in a.get("options", []) or []:
            parts.append(str(opt))
    return _normalize(" ".join(parts))

def _match_subtipo(texto: str, subtipo: Optional[str]) -> bool:
    if not subtipo:
        return True
    if subtipo == "guayabera":
        return ("guayabera" in texto)
    return True

def _match_manga(texto: str, manga: Optional[str]) -> bool:
    if not manga:
        return True
    if manga == "larga":
        return ("manga larga" in texto) and ("manga corta" not in texto)
    if manga == "corta":
        return ("manga corta" in texto) and ("manga larga" not in texto)
    return True

def _match_color(texto: str, color: Optional[str]) -> bool:
    if not color:
        return True
    return color in texto

def detectar_categoria(texto_usuario: str) -> Tuple[str | None, float]:
    tokens = [_normalize(t) for t in _tokenize(texto_usuario)]

    for t in tokens:
        if t in SYNONYMS:
            return SYNONYMS[t], 1.0
        if t in CATEGORY_IDS:
            return t, 1.0

    universe = list(CATEGORY_IDS.keys()) + list(SYNONYMS.keys())
    joined   = " ".join(tokens)

    best, score, _ = process.extractOne(
        joined, universe, scorer=fuzz.token_sort_ratio
    ) or (None, 0, None)

    if score >= 80:
        return SYNONYMS.get(best, best), score / 100.0

    return None, 0.0

def get_products(category: str, max_items: int = 10) -> List[dict]:
    cat_id = CATEGORY_IDS.get(category)
    if not cat_id:
        return []
    items = _woo_get("products", {
        "category": cat_id,
        "per_page": max(50, max_items),
    })
    return _filtrar_stock(items)

def get_variaciones(product_id: int) -> List[dict]:
    """Devuelve variaciones activas (por ejemplo, tallas disponibles) de un producto."""
    try:
        variaciones = _woo_get(f"products/{product_id}/variations", {"per_page": 20})
        return [
            v for v in variaciones
            if v.get("stock_status") == "instock"
               and float(v.get("price", 0)) > 0
        ]
    except Exception:
        return []

def sugerir_productos(
    texto_usuario: str,
    limite: int = 3,
    excluir_urls: Optional[List[str]] = None,
    incluye_palabras: Optional[set] = None,
    excluye_palabras: Optional[set] = None
) -> Dict:
    """
    Devuelve hasta 'limite' productos de la categoría detectada,
    filtrados por atributos mencionados (manga/subtipo/color) y por
    palabras clave inclusivas/exclusivas opcionales.
    'excluir_urls' permite no repetir sugerencias anteriores.
    """
    categoria, conf = detectar_categoria(texto_usuario)
    if not categoria:
        return {"mensaje": "No detecté ninguna categoría concreta."}

    attrs = detectar_atributos(texto_usuario)

    # normaliza sets de include/exclude
    incluye = {_normalize(x) for x in (incluye_palabras or set())}
    excluye = {_normalize(x) for x in (excluye_palabras or set())}

    def _pasa_sets(texto_norm: str) -> bool:
        if any(k in texto_norm for k in excluye):
            return False
        if incluye and not any(k in texto_norm for k in incluye):
            return False
        return True

    def _filtra_lista(items: List[dict], urls_fuera: set) -> List[dict]:
        out = []
        for p in items:
            if p.get("permalink") in urls_fuera:
                continue
            txt = _texto_de_producto(p)  # ya normalizado
            if not _match_subtipo(txt, attrs.get("subtipo")):
                continue
            if not _match_manga(txt, attrs.get("manga")):
                continue
            if not _match_color(txt, attrs.get("color")):
                continue
            if not _pasa_sets(txt):
                continue
            out.append(p)
        return out

    urls_fuera = set(excluir_urls or [])

    # 1) Trae por categoría detectada
    base = get_products(categoria, max_items=max(limite, 20))
    if not base:
        return {"mensaje": f"No hay stock en la categoría «{categoria}» ahora mismo."}

    candidatos = _filtra_lista(base, urls_fuera)

    # 2) Fallback especial: si pidieron guayabera y no hay, intenta en camisas manteniendo filtros
    if not candidatos and attrs.get("subtipo") == "guayabera" and categoria != "camisas":
        base2 = get_products("camisas", max_items=20)
        candidatos = _filtra_lista(base2 or [], urls_fuera)

    if not candidatos:
        # arma mensaje humano con los atributos pedidos
        human_attrs = []
        if attrs.get("subtipo") == "guayabera":
            human_attrs.append("guayabera")
        if attrs.get("manga"):
            human_attrs.append(f"manga {attrs['manga']}")
        if attrs.get("color"):
            human_attrs.append(attrs["color"])
        detalle = " ".join(human_attrs) if human_attrs else categoria
        return {"mensaje": f"No hay stock para «{detalle}» en este momento."}

    # 3) Armar respuesta con variaciones/tallas
    productos_detalle = []
    for p in candidatos[:limite]:
        variaciones = get_variaciones(p["id"])
        tallas = []
        for v in variaciones:
            if v.get("attributes"):
                opt = v["attributes"][0].get("option")
                if opt:
                    tallas.append(opt)
        productos_detalle.append({
            "nombre": p.get("name", ""),
            "precio": float(p.get("price", 0)),
            "url": p.get("permalink", ""),
            "tallas_disponibles": tallas
        })

    return {
        "categoria_detectada": categoria,
        "atributos_detectados": attrs,
        "productos": productos_detalle
    }
