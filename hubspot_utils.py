# hubspot_utils.py — v3.0 (Contacts upsert + Task creation/association)
import os
import re
import time
import json
import hashlib
import requests
from typing import Optional, Dict, Any, List, Tuple
from dotenv import load_dotenv

load_dotenv()

# ========= Config =========
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip()  # Private App Token
HS_DEFAULT_OWNER_ID = os.getenv("HS_DEFAULT_OWNER_ID", "").strip()          # opcional
HS_DESPACHOS_QUEUE_ID = os.getenv("HS_DESPACHOS_QUEUE_ID", "").strip()      # opcional

# Endpoints
BASE_CONTACTS = "https://api.hubapi.com/crm/v3/objects/contacts"
SEARCH_CONTACTS = "https://api.hubapi.com/crm/v3/objects/contacts/search"
BASE_TASKS = "https://api.hubapi.com/crm/v3/objects/tasks"
SEARCH_TASKS = "https://api.hubapi.com/crm/v3/objects/tasks/search"
PROP_TASKS = "https://api.hubapi.com/crm/v3/properties/tasks"
PROP_TASKS_ONE = "https://api.hubapi.com/crm/v3/properties/tasks/{prop}"
ASSOC_LABELS = "https://api.hubapi.com/crm/v4/associations/{from_obj}/{to_obj}/labels"
ASSOC_OBJECTS = "https://api.hubapi.com/crm/v4/objects/{from_obj}/{from_id}/associations/{to_obj}/{to_id}"

# Caches simples
_ASSOC_TYPE_CACHE: Dict[str, int] = {}
_PROP_CREATED_CACHE: set = set()

# ---------- Helpers HTTP ----------

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }

def _request_with_retry(method: str, url: str, *, json_payload=None, params=None, headers=None, timeout=15, retries=3, backoff=0.6):
    """
    Wrapper con reintentos para 429/5xx.
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

    # Si ya viene con indicativo 57 y 10 dígitos nacionales
    if digits.startswith("57") and len(digits) >= 12:
        base10 = digits[-10:]
        plain57 = f"57{base10}"
        plus57 = f"+57{base10}"
        return plain57, plus57

    # Si empieza por 3 u 0 (nacionales) y tiene >=10 dígitos -> agrega 57
    if digits.startswith(("3", "0")) and len(digits) >= 10:
        base10 = digits[-10:]
        return f"57{base10}", f"+57{base10}"

    # Fallback
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

# ---------- Búsqueda Contacto ----------

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
        r = _request_with_retry("POST", SEARCH_CONTACTS, json_payload=body, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results", [])
        if results:
            return results[0].get("id")
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot search contact error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot search contact error:", repr(e))
        except Exception:
            print("❌ HubSpot search contact error:", repr(e))
    return None

# ---------- Mapeo de propiedades (Contacto) ----------

def _prepare_contact_properties(pedido) -> Dict[str, Any]:
    """
    Mapea el pedido a propiedades de HubSpot (Contacto).
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
        "phone": plus57 or plain57,
        "mobilephone": plus57 or plain57,
        "address": direccion,
        "city": ciudad,
        # Si estás usando estas custom en Contacto, se mantienen:
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

    cleaned = {k: v for k, v in props.items() if v not in (None, "", " ")}
    return cleaned

# ---------- Contact Upsert ----------

def _upsert_contact_and_get_id(pedido) -> Optional[str]:
    if not HUBSPOT_TOKEN:
        print("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
        return None

    props = _prepare_contact_properties(pedido)
    email = props.get("email", "")
    phone_raw = getattr(pedido, "telefono", None) or _safe(getattr(pedido, "session_id", "")).replace("cliente_", "")
    phone_variants = _norm_phone_variants(phone_raw)

    # 1) Buscar contacto existente
    contact_id = _search_contact(email=email, phone_variants=phone_variants)

    try:
        if contact_id:
            # UPDATE
            url = f"{BASE_CONTACTS}/{contact_id}"
            r = _request_with_retry("PATCH", url, json_payload={"properties": props}, timeout=10)
            r.raise_for_status()
            print(f"✅ HubSpot UPDATE OK (contact {contact_id})")
            return contact_id
        else:
            # CREATE
            r = _request_with_retry("POST", BASE_CONTACTS, json_payload={"properties": props}, timeout=10)
            r.raise_for_status()
            cid = (r.json() or {}).get("id")
            print(f"✅ HubSpot CREATE OK (contact {cid})")
            return cid
    except requests.HTTPError as e:
        try:
            print("❌ HubSpot HTTP error (contact upsert):", e, e.response.status_code, e.response.text)
        except Exception:
            print("❌ HubSpot HTTP error (contact upsert):", repr(e))
    except Exception as e:
        print("❌ HubSpot error inesperado (contact upsert):", repr(e))
    return None

# ---------- Helpers Tasks ----------

def _ensure_task_property_order_id():
    """
    Garantiza que exista la propiedad custom `order_id` en Tasks (texto).
    """
    cache_key = "tasks.order_id"
    if cache_key in _PROP_CREATED_CACHE:
        return
    # ¿Existe?
    try:
        url = PROP_TASKS_ONE.format(prop="order_id")
        r = _request_with_retry("GET", url, timeout=10)
        if 200 <= r.status_code < 300:
            _PROP_CREATED_CACHE.add(cache_key)
            return
    except Exception:
        pass

    # Si no existe, se crea
    body = {
        "name": "order_id",
        "label": "Order ID",
        "type": "string",
        "fieldType": "text",
        "description": "Identificador único del pedido (para idempotencia)",
        "groupName": "taskinformation"
    }
    try:
        _request_with_retry("POST", PROP_TASKS, json_payload=body, timeout=10)
    except Exception:
        # Si otro proceso la creó en carrera, ignoramos
        pass
    _PROP_CREATED_CACHE.add(cache_key)

def _find_task_by_order_id(order_id: str) -> Optional[str]:
    if not order_id:
        return None
    _ensure_task_property_order_id()
    body = {
        "filterGroups": [{"filters": [{"propertyName": "order_id", "operator": "EQ", "value": str(order_id)}]}],
        "properties": ["hs_task_subject", "order_id"],
        "limit": 1
    }
    try:
        r = _request_with_retry("POST", SEARCH_TASKS, json_payload=body, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results", [])
        if results:
            return results[0].get("id")
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot search task error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot search task error:", repr(e))
        except Exception:
            print("❌ HubSpot search task error:", repr(e))
    return None

def _create_task(props: Dict[str, Any]) -> Optional[str]:
    try:
        r = _request_with_retry("POST", BASE_TASKS, json_payload={"properties": props}, timeout=10)
        r.raise_for_status()
        return (r.json() or {}).get("id")
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot create task error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot create task error:", repr(e))
        except Exception:
            print("❌ HubSpot create task error:", repr(e))
    return None

def _get_assoc_type_id(from_obj: str, to_obj: str) -> int:
    """
    Obtiene y cachea el associationTypeId por defecto entre dos objetos (p.ej., tasks -> contacts).
    """
    key = f"{from_obj}->{to_obj}"
    if key in _ASSOC_TYPE_CACHE:
        return _ASSOC_TYPE_CACHE[key]
    url = ASSOC_LABELS.format(from_obj=from_obj, to_obj=to_obj)
    r = _request_with_retry("GET", url, timeout=10)
    r.raise_for_status()
    data = r.json() or {}
    results = data.get("results") or []
    # Toma el primer HUBSPOT_DEFINED
    for it in results:
        if it.get("category") == "HUBSPOT_DEFINED" and isinstance(it.get("typeId"), int):
            _ASSOC_TYPE_CACHE[key] = it["typeId"]
            return it["typeId"]
    # Fallback: el primero
    if results and isinstance(results[0].get("typeId"), int):
        _ASSOC_TYPE_CACHE[key] = results[0]["typeId"]
        return results[0]["typeId"]
    raise RuntimeError(f"No se halló associationTypeId para {from_obj}->{to_obj}")

def _associate_objects(from_obj: str, from_id: str, to_obj: str, to_id: str):
    type_id = _get_assoc_type_id(from_obj, to_obj)
    url = ASSOC_OBJECTS.format(from_obj=from_obj, from_id=from_id, to_obj=to_obj, to_id=to_id)
    body = {"types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}
    try:
        r = _request_with_retry("PUT", url, json_payload=body, timeout=10)
        r.raise_for_status()
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot association error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot association error:", repr(e))
        except Exception:
            print("❌ HubSpot association error:", repr(e))

# ---------- Helpers de Tarea (contenido) ----------

def _fmt_money_cop(v) -> str:
    try:
        return f"${float(v):,.0f}".replace(",", ".")
    except Exception:
        return "$0"

def _build_order_id(pedido) -> str:
    """
    Prefiere numero_confirmacion; si no, usa un ID estable por sesión.
    """
    num = _safe(getattr(pedido, "numero_confirmacion", None))
    if num:
        return num
    sid = _safe(getattr(pedido, "session_id", None))
    basis = sid or f"anon-{int(time.time())}"
    return "SID-" + hashlib.md5(basis.encode("utf-8")).hexdigest()[:10]

def _compose_task_subject(pedido) -> str:
    nombre = _safe(getattr(pedido, "nombre_cliente", None))
    return f"Despachar pedido {_build_order_id(pedido)} — {nombre or 'Cliente WhatsApp'}"

def _compose_task_body(pedido, cart_items: List[dict]) -> str:
    lines = []
    lines.append(f"Pedido: {_build_order_id(pedido)}")

    nombre = _safe(getattr(pedido, "nombre_cliente", None))
    email  = _safe(getattr(pedido, "email", None))
    tel    = _safe(getattr(pedido, "telefono", None))
    ciudad = _safe(getattr(pedido, "ciudad", None))
    dir1   = _safe(getattr(pedido, "direccion", None))

    if nombre: lines.append(f"Cliente: {nombre}")
    if email:  lines.append(f"Email: {email}")
    if tel:    lines.append(f"Teléfono: {tel}")
    if ciudad or dir1:
        lines.append(f"Envío: {ciudad or '—'} | {dir1 or '—'}")

    metodo_entrega = _safe(getattr(pedido, "metodo_entrega", None)).replace("_", " ")
    metodo_pago = _safe(getattr(pedido, "metodo_pago", None)).replace("_", " ")
    estado = _safe(getattr(pedido, "estado", None))
    if metodo_entrega: lines.append(f"Método entrega: {metodo_entrega}")
    if metodo_pago:    lines.append(f"Método pago: {metodo_pago}")
    if estado:         lines.append(f"Estado: {estado}")

    # Resumen carrito
    if cart_items:
        lines.append("Items:")
        total = 0.0
        for it in cart_items:
            nombre_it = it.get("nombre") or it.get("sku") or "Item"
            qty = int(it.get("cantidad", 1))
            talla = it.get("talla")
            color = it.get("color")
            unit = float(it.get("precio_unitario", 0.0))
            total += unit * qty
            tail = " ".join([x for x in [color or "", talla or ""] if x]).strip()
            tail = f" ({tail})" if tail else ""
            lines.append(f" - {qty}× {nombre_it}{tail} – {_fmt_money_cop(unit)} c/u")
        lines.append(f"Total estimado: {_fmt_money_cop(total)}")

    lines.append("\nSLA: Contactar al cliente ANTES de despachar.")
    return "\n".join(lines)

# ---------- API principal ----------

def enviar_pedido_a_hubspot(pedido) -> bool:
    """
    1) Upsert de Contacto (email/teléfono).
    2) Idempotencia: buscar Task con order_id (numero_confirmacion o hash de sesión).
    3) Crear Task (subject/body, due date +2h, queue/owner opcional).
    4) Asociar Task ↔ Contact.
    """
    if not HUBSPOT_TOKEN:
        print("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
        return False

    # 1) Contacto
    contact_id = _upsert_contact_and_get_id(pedido)
    if not contact_id:
        # No bloquea creación de tarea si contacto falla, pero lo intentamos igual
        print("⚠️ No se obtuvo contact_id; continúo con Task sin asociación (se reintenta luego).")

    # 2) Idempotencia por order_id
    order_id = _build_order_id(pedido)
    existing_task_id = _find_task_by_order_id(order_id)
    if existing_task_id:
        # Asegura asociación con Contacto
        if contact_id:
            try:
                _associate_objects("tasks", existing_task_id, "contacts", contact_id)
            except Exception:
                pass
        print(f"ℹ️ Task ya existía para order_id={order_id} (id={existing_task_id})")
        return True

    # 3) Crear Task
    # Carga carrito
    try:
        cart_items = json.loads(getattr(pedido, "carrito_json", "[]") or "[]")
        if not isinstance(cart_items, list):
            cart_items = []
    except Exception:
        cart_items = []

    subject = _compose_task_subject(pedido)
    body    = _compose_task_body(pedido, cart_items)

    props: Dict[str, Any] = {
        "hs_task_subject": subject,
        "hs_task_body": body,
        "order_id": str(order_id),
        # Due date: ahora + 2h (epoch ms)
        "hs_task_due_date": int(time.time() * 1000) + 2 * 60 * 60 * 1000,
    }
    if HS_DEFAULT_OWNER_ID:
        props["hubspot_owner_id"] = HS_DEFAULT_OWNER_ID
    if HS_DESPACHOS_QUEUE_ID:
        props["hs_task_queue_id"] = HS_DESPACHOS_QUEUE_ID

    task_id = _create_task(props)
    if not task_id:
        return False

    # 4) Asociar Task ↔ Contact
    if contact_id:
        try:
            _associate_objects("tasks", task_id, "contacts", contact_id)
        except Exception:
            pass

    print(f"✅ HubSpot TASK creada id={task_id} order_id={order_id}")
    return True
