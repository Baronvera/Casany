# hubspot_utils.py — v2.2
import os
import re
import requests
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")
BASE = "https://api.hubapi.com/crm/v3/objects/contacts"

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }

def _norm_phone(raw: Optional[str]) -> str:
    """Normaliza a dígitos y asegura indicativo COL (+57) si falta."""
    if not raw:
        return ""
    # quita todo menos dígitos
    digits = re.sub(r"\D", "", raw)
    # si empieza por 57 ya está; si empieza por 0 ó 3 (nacionales), anteponer 57
    if digits.startswith("57"):
        return digits
    if digits.startswith(("3", "0")) and len(digits) >= 10:
        return "57" + digits[-10:]  # usa los últimos 10 díg.
    return digits

def _build_email(phone57: str) -> str:
    """Email sintético estable para upsert por email."""
    return f"{phone57 or 'sin_telefono'}@cassany.co"

def _safe(s: Optional[str]) -> str:
    return (s or "").strip()

def _search_contact(email: str, phone57: str) -> Optional[str]:
    """Busca contacto por email o teléfono. Devuelve contactId o None."""
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    q = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]},
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone57}]},
        ],
        "properties": ["email", "phone"],
        "limit": 1,
    }
    try:
        r = requests.post(url, headers=_headers(), json=q, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results", [])
        if results:
            return results[0].get("id")
    except Exception as e:
        print("❌ HubSpot search error:", e)
    return None

def _prepare_properties(pedido) -> Dict[str, Any]:
    """Mapea el pedido a propiedades de HubSpot."""
    telefono = _norm_phone(getattr(pedido, "telefono", None) or _safe(getattr(pedido, "session_id", "")).replace("cliente_", ""))
    email    = _safe(getattr(pedido, "email", "")) or _build_email(telefono)
    nombre   = _safe(getattr(pedido, "nombre_cliente", None))
    direccion= _safe(getattr(pedido, "direccion", None))
    ciudad   = _safe(getattr(pedido, "ciudad", None))
    producto = _safe(getattr(pedido, "producto", None))
    talla    = _safe(getattr(pedido, "talla", None))
    cantidad = str(getattr(pedido, "cantidad", 0) or 0)
    metodo_pago = _safe(getattr(pedido, "metodo_pago", None))
    metodo_entrega = _safe(getattr(pedido, "metodo_entrega", None)).lower()
    numero_confirmacion = _safe(getattr(pedido, "numero_confirmacion", None))
    estado   = _safe(getattr(pedido, "estado", "pendiente"))
    punto_venta = _safe(getattr(pedido, "punto_venta", None))

    props = {
        "email": email,
        "firstname": nombre or "Cliente",
        "phone": telefono,
        "address": direccion,
        "city": ciudad,
        # Propiedades personalizadas (deben existir en HubSpot con estos internal names)
        "custom_cas_producto": producto,
        "custom_cas_talla": talla,
        "custom_cas_cantidad": cantidad,
        "custom_cas_metodo_pago": metodo_pago,
        "custom_cas_metodo_entrega": metodo_entrega,
        "custom_cas_numero_confirmacion": numero_confirmacion,
        "custom_cas_estado": estado,
    }

    if metodo_entrega == "recoger_en_tienda" and punto_venta:
        props["custom_cas_punto_de_venta"] = punto_venta

    return props

def enviar_pedido_a_hubspot(pedido) -> bool:
    """
    Crea o actualiza (upsert) el contacto en HubSpot a partir del pedido.
    - Busca por email y teléfono.
    - Si existe, hace PATCH.
    - Si no, hace POST (create).
    Devuelve True si fue exitoso.
    """
    if not HUBSPOT_TOKEN:
        print("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
        return False

    props = _prepare_properties(pedido)
    email = props.get("email", "")
    phone = props.get("phone", "")

    # 1) Buscar contacto existente
    contact_id = _search_contact(email=email, phone57=phone)

    try:
        if contact_id:
            # UPDATE
            url = f"{BASE}/{contact_id}"
            r = requests.patch(url, headers=_headers(), json={"properties": props}, timeout=10)
            r.raise_for_status()
            print(f"✅ Contacto actualizado en HubSpot (ID {contact_id}).")
            return True
        else:
            # CREATE
            r = requests.post(BASE, headers=_headers(), json={"properties": props}, timeout=10)
            r.raise_for_status()
            cid = (r.json() or {}).get("id")
            print(f"✅ Contacto creado en HubSpot (ID {cid}).")
            return True
    except requests.HTTPError as e:
        # Mostrar respuesta de error de HubSpot (útil para propiedades inexistentes, etc.)
        try:
            print("❌ Error HubSpot:", e.response.status_code, e.response.text)
        except Exception:
            print("❌ Error HubSpot:", e)
    except Exception as e:
        print("❌ Error inesperado HubSpot:", e)

    return False

