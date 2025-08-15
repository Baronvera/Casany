# woocommerce_client.py — v2.2
import os
from typing import Any, Dict, List, Optional, Union

import requests
from dotenv import load_dotenv
from urllib.parse import urlencode

load_dotenv()

WC_API_URL: str = os.getenv("WOOCOMMERCE_API_URL", "").rstrip("/") + "/"
WC_CONSUMER_KEY: Optional[str] = os.getenv("WOOCOMMERCE_CONSUMER_KEY")
WC_CONSUMER_SECRET: Optional[str] = os.getenv("WOOCOMMERCE_CONSUMER_SECRET")

# Validación básica
if not WC_API_URL or not WC_CONSUMER_KEY or not WC_CONSUMER_SECRET:
    # No lanzamos excepción para no romper import; devolvemos errores en runtime
    pass

# Por convención, espera que WC_API_URL apunte a .../wp-json/wc/v3/
# Ejemplo: https://tutienda.com/wp-json/wc/v3/


def _auth_params(**extra: Any) -> Dict[str, Any]:
    """Mezcla credenciales con parámetros extra."""
    base = {
        "consumer_key": WC_CONSUMER_KEY,
        "consumer_secret": WC_CONSUMER_SECRET,
    }
    base.update(extra or {})
    return base


def _endpoint(path: str) -> str:
    """Construye URL segura evitando //"""
    return WC_API_URL + path.lstrip("/")


def _request(method: str, path: str, params: Optional[Dict[str, Any]] = None, timeout: Union[int, float] = 10) -> Union[List[Any], Dict[str, Any]]:
    """Invoca la API de WooCommerce y devuelve JSON o {'error': ...}."""
    if not WC_CONSUMER_KEY or not WC_CONSUMER_SECRET or not WC_API_URL:
        return {"error": "WooCommerce credentials or base URL missing."}

    url = _endpoint(path)
    try:
        resp = requests.request(method.upper(), url, params=_auth_params(**(params or {})), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def get_all_products(per_page: int = 20, stock_only: bool = False, max_pages: int = 1) -> Union[List[Dict[str, Any]], Dict[str, str]]:
    """
    Lista productos. Usa paginación si max_pages > 1 (Woo máx per_page=100).
    stock_only: si True, filtra a mano por 'instock'.
    """
    results: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = _request("GET", "products", {"per_page": per_page, "page": page, "status": "publish"})
        if isinstance(data, dict) and data.get("error"):
            return data
        batch = data or []
        if stock_only:
            batch = [p for p in batch if str(p.get("stock_status")) == "instock" and float(p.get("price") or 0) > 0]
        results.extend(batch)
        if len(batch) < per_page:
            break  # última página
    return results


def get_products_by_category(cat_id: int, per_page: int = 10, stock_only: bool = False, max_pages: int = 1) -> Union[List[Dict[str, Any]], Dict[str, str]]:
    """
    Lista productos por categoría (ID de Woo). Soporta paginación y filtro de stock.
    """
    results: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = _request("GET", "products", {"category": cat_id, "per_page": per_page, "page": page, "status": "publish"})
        if isinstance(data, dict) and data.get("error"):
            return data
        batch = data or []
        if stock_only:
            batch = [p for p in batch if str(p.get("stock_status")) == "instock" and float(p.get("price") or 0) > 0]
        results.extend(batch)
        if len(batch) < per_page:
            break
    return results


def get_product_by_id(product_id: int) -> Union[Dict[str, Any], Dict[str, str]]:
    """Obtiene un producto específico por ID."""
    return _request("GET", f"products/{product_id}")


def get_variations(product_id: int, per_page: int = 50, stock_only: bool = False, max_pages: int = 1) -> Union[List[Dict[str, Any]], Dict[str, str]]:
    """
    Lista variaciones de un producto. Filtra stock si se indica.
    """
    results: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = _request("GET", f"products/{product_id}/variations", {"per_page": per_page, "page": page})
        if isinstance(data, dict) and data.get("error"):
            return data
        batch = data or []
        if stock_only:
            batch = [v for v in batch if str(v.get("stock_status")) == "instock" and float(v.get("price") or 0) > 0]
        results.extend(batch)
        if len(batch) < per_page:
            break
    return results


# Prueba rápida manual
if __name__ == "__main__":
    prods = get_all_products(per_page=20, stock_only=True, max_pages=1)
    if isinstance(prods, dict) and prods.get("error"):
        print("ERROR:", prods["error"])
    else:
        for p in prods[:10]:
            print(f"- {p.get('name')} (ID: {p.get('id')}) – ${p.get('price')}")

