# hubspot_utils.py — v2.3
import os
import re
import time
import json
import hashlib
import requests
from typing import Optional, Dict, Any, List, Tuple
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
BASE = "https://api.hubapi.com/crm/v3/objects/contacts"
SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/contacts/search"

# ---------- Helpers HTTP ----------

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }

def _request_with_retry(method: str, url: str, *, json_payload=None, params=None, headers=None, timeout=10, retries=3, backoff=0.6):
    """
    Pequeño wrapper con reintentos para 429/5xx.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, headers=headers or _headers(), json=json_payload, params=params, timeout=timeout)
            # Reintentar en 429 y 5xx
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            return r
        except requests.HTTPError as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise
    if last_exc:
        raise last_exc

# ---------- Normalizadores ----------

def _safe(s: Optional[str]) -> str:
    return (s or "").strip()

def _norm_phone_variants(raw: Optional[str]) -> Tuple[str, str]:
    """
    Normaliza a dos variantes comunes para búsqueda:
    - plain57: '57##########'
    - plus57:  '+57##########'
    Retorna ('', '') si no hay suficientes dígitos.
    """
    if not raw:
        return "", ""
    digits = re.sub(r"\D", "", raw)

    # Si ya viene con indicativo 57 y 10 dígitos nacionales -> ok
    if digits.startswith("57") and len(digits) >= 12:
        base10 = digits[-10:]
        plain57 = f"57{base10}"
        plus57 = f"+57{base10}"
        return plain57, plus57

    # Si empieza por 3 u 0 (nacionales) y tiene >=10 dígitos -> agrega 57
    if digits.startswith(("3", "0")) and len(digits) >= 10:
        base10 = digits[-10:]
        return f"57{base10}", f"+57{base10}"

    # Si no calza, igual intentamos como está + con '+'
    if digits:
        return digits, f"+{digits}"
    return "", ""

def _build_email(phone57_plain: str, session_id: Optional[str]) -> str:
    """
    Email sintético estable:
    - Si hay teléfono: tel57@cassany.co
    - Si no: usa hash corto del session_id: sid_<hash7>@cassany.co
    - Si no hay nada: guest_<epoch>@cassany.co (muy improbable)
    """
    if phone57_plain:
        return f"{phone57_plain}@cassany.co"
    sid = _safe(session_id)
    if sid:
        h = hashlib.sha1(sid.encode("utf-8")).hexdigest()[:7]
        return f"sid_{h}@cassany.co"
    return f"guest_{int(time.time())}@cassany.co"

# ---------- Búsqueda ----------

def _build_search_body(email: str, phone_variants: Tuple[str, str]) -> Dict[str, Any]:
    """
    Construye un cuerpo de búsqueda con OR entre grupos:
    - email == email
    - phone == variante1
    - phone == variante2
    - mobilephone == variante1
    - mobilephone == variante2
    """
    plain57, plus57 = phone_variants
    groups = []

    if email:
        groups.append({"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]})

    def _add_phone_group(prop: str, value: str):
        if value:
            groups.append({"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]})

    # Variantes de phone
    _add_phone_group("phone", plain57)
    _add_phone_group("phone", plus57)
    _add_phone_group("mobilephone", plain57)
    _add_phone_group("mobilephone", plus57)

    # Si por alguna razón no hay grupos, evita 400 del API
    if not groups:
        groups.append({"filters": [{"propertyName": "email", "operator": "HAS_PROPERTY"}]})

    return {
        "filterGroups": groups,  # OR entre grupos
        "properties": ["email", "firstname", "phone", "mobilephone"],
        "limit": 1,
    }

def _search_contact(email: str, phone_variants: Tuple[str, str]) -> Optional[str]:
    """
    Busca contacto por email o teléfono/móvil (variantes).
    Devuelve contactId o None.
    """
    try:
        body = _build_search_body(email, phone_variants)
        r = _request_with_retry("POST", SEARCH_URL, json_payload=body, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results", [])
        if results:
            return results[0].get("id")
    except Exception as e:
        try:
            # imprime cuerpo del error si viene
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot search error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot search error:", repr(e))
        except Exception:
            print("❌ HubSpot search error:", repr(e))
    return None

# ---------- Mapeo de propiedades ----------

def _prepare_properties(pedido) -> Dict[str, Any]:
    """
    Mapea el pedido a propiedades de HubSpot.
    NOTA: Se dejan tus custom properties tal cual.
    """
    telefono_raw = getattr(pedido, "telefono", None) or _safe(getattr(pedido, "session_id", "")).replace("cliente_", "")
    plain57, plus57 = _norm_phone_variants(telefono_raw)
    email_real = _safe(getattr(pedido, "email", ""))
    email = email_real or _build_email(plain57, getattr(pedido, "session_id", None))

    nombre   = _safe(getattr(pedido, "nombre_cliente", None)) or "Cliente"
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

    props: Dict[str, Any] = {
        "email": email,
        "firstname": nombre,
        # Preferimos `mobilephone` para celulares en LATAM; si ya usas `phone`, puedes duplicar:
        "phone": plus57 or plain57,
        "mobilephone": plus57 or plain57,
        "address": direccion,
        "city": ciudad,
        # Custom (asegúrate de que existen en HubSpot con estos internal names):
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

    # Limpia claves vacías redundantes (opcional)
    cleaned = {k: v for k, v in props.items() if v not in (None, "", " ")}
    return cleaned

# ---------- Upsert ----------

def enviar_pedido_a_hubspot(pedido) -> bool:
    """
    Crea o actualiza (upsert) el contacto en HubSpot a partir del pedido.
    - Busca por email y teléfono (phone y mobilephone, con variantes +57/57)
    - Si existe, PATCH.
    - Si no existe, POST (create).
    Devuelve True si fue exitoso.
    """
    if not HUBSPOT_TOKEN:
        print("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
        return False

    props = _prepare_properties(pedido)
    email = props.get("email", "")
    phone_raw = getattr(pedido, "telefono", None) or _safe(getattr(pedido, "session_id", "")).replace("cliente_", "")
    phone_variants = _norm_phone_variants(phone_raw)

    context_id = _safe(getattr(pedido, "session_id", "")) or email

    # 1) Buscar contacto existente
    contact_id = _search_contact(email=email, phone_variants=phone_variants)

    try:
        if contact_id:
            # UPDATE
            url = f"{BASE}/{contact_id}"
            r = _request_with_retry("PATCH", url, json_payload={"properties": props}, timeout=10)
            r.raise_for_status()
            print(f"✅ HubSpot UPDATE OK (contact {contact_id}) ctx={context_id}")
            return True
        else:
            # CREATE
            r = _request_with_retry("POST", BASE, json_payload={"properties": props}, timeout=10)
            r.raise_for_status()
            cid = (r.json() or {}).get("id")
            print(f"✅ HubSpot CREATE OK (contact {cid}) ctx={context_id}")
            return True
    except requests.HTTPError as e:
        try:
            print("❌ HubSpot HTTP error:", e, e.response.status_code, e.response.text)
        except Exception:
            print("❌ HubSpot HTTP error:", repr(e))
    except Exception as e:
        print("❌ HubSpot error inesperado:", repr(e))

    return False
