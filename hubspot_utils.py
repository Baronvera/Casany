# hubspot_utils.py — v4.0 (Contacts upsert + DEAL creation/association + Task opcional SLA)
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

# IDs REALES requeridos (no uses "default" a menos que sea el ID real en tu portal)
HS_DEAL_PIPELINE_ID = os.getenv("HS_DEAL_PIPELINE_ID", "").strip()          # p.ej. "default" si ESE es el ID real
HS_DEAL_STAGE_ID = os.getenv("HS_DEAL_STAGE_ID", "").strip()                # p.ej. "appointmentscheduled" o personalizado

# Feature flag: crear además una Task operativa (SLA +2h) al crear el Deal
HS_CREATE_SLA_TASK = (os.getenv("HS_CREATE_SLA_TASK", "true").strip().lower() == "true")

# Endpoints
BASE_CONTACTS = "https://api.hubapi.com/crm/v3/objects/contacts"
SEARCH_CONTACTS = "https://api.hubapi.com/crm/v3/objects/contacts/search"

BASE_DEALS = "https://api.hubapi.com/crm/v3/objects/deals"
SEARCH_DEALS = "https://api.hubapi.com/crm/v3/objects/deals/search"
PROP_DEALS = "https://api.hubapi.com/crm/v3/properties/deals"
PROP_DEALS_ONE = "https://api.hubapi.com/crm/v3/properties/deals/{prop}"

# Para la Task opcional
BASE_TASKS = "https://api.hubapi.com/crm/v3/objects/tasks"

# Associations v4
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
    Mantiene contacto "limpio"; los datos transaccionales van al Deal.
    """
    telefono_raw = getattr(pedido, "telefono", None) or _safe(getattr(pedido, "session_id", "")).replace("cliente_", "")
    plain57, plus57 = _norm_phone_variants(telefono_raw)
    email_real = _safe(getattr(pedido, "email", ""))
    email = email_real or _build_email(plain57, getattr(pedido, "session_id", None))

    nombre   = _safe(getattr(pedido, "nombre_cliente", None)) or "Cliente"
    direccion= _safe(getattr(pedido, "direccion", None))
    ciudad   = _safe(getattr(pedido, "ciudad", None))

    props: Dict[str, Any] = {
        "email": email,
        "firstname": nombre,
        "phone": plus57 or plain57,
        "mobilephone": plus57 or plain57,
        "address": direccion,
        "city": ciudad,
    }
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

# ---------- Helpers Deals ----------

def _ensure_deal_property_order_id():
    """
    Garantiza que exista la propiedad custom `order_id` en Deals (texto).
    """
    cache_key = "deals.order_id"
    if cache_key in _PROP_CREATED_CACHE:
        return
    # ¿Existe?
    try:
        url = PROP_DEALS_ONE.format(prop="order_id")
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
        "groupName": "dealinformation"
    }
    try:
        _request_with_retry("POST", PROP_DEALS, json_payload=body, timeout=10)
    except Exception:
        # Si otro proceso la creó en carrera, ignoramos
        pass
    _PROP_CREATED_CACHE.add(cache_key)

def _find_deal_by_order_id(order_id: str) -> Optional[str]:
    if not order_id:
        return None
    _ensure_deal_property_order_id()
    body = {
        "filterGroups": [{"filters": [{"propertyName": "order_id", "operator": "EQ", "value": str(order_id)}]}],
        "properties": ["dealname", "order_id", "amount"],
        "limit": 1
    }
    try:
        r = _request_with_retry("POST", SEARCH_DEALS, json_payload=body, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results", [])
        if results:
            return results[0].get("id")
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot search deal error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot search deal error:", repr(e))
        except Exception:
            print("❌ HubSpot search deal error:", repr(e))
    return None

def _create_deal(props: Dict[str, Any]) -> Optional[str]:
    try:
        r = _request_with_retry("POST", BASE_DEALS, json_payload={"properties": props}, timeout=10)
        r.raise_for_status()
        return (r.json() or {}).get("id")
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot create deal error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot create deal error:", repr(e))
        except Exception:
            print("❌ HubSpot create deal error:", repr(e))
    return None

# ---------- Helpers Tasks (opcional SLA) ----------

def _create_task(props: Dict[str, Any]) -> Optional[str]:
    """
    Crea una Task simple (se usa SOLO si HS_CREATE_SLA_TASK=true).
    """
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

# ---------- Associations ----------

def _get_assoc_type_id(from_obj: str, to_obj: str) -> int:
    """
    Obtiene y cachea el associationTypeId por defecto entre dos objetos (p.ej., deals -> contacts).
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

# ---------- Helpers de Deal (contenido) ----------

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

def _compose_deal_name(pedido) -> str:
    nombre = _safe(getattr(pedido, "nombre_cliente", None))
    return f"Pedido {_build_order_id(pedido)} — {nombre or 'Cliente WhatsApp'}"

def _prepare_deal_properties(pedido, cart_items: List[dict]) -> Dict[str, Any]:
    """
    Prepara las propiedades para crear un Deal en HubSpot (pipeline/etapa correctos).
    """
    nombre = _safe(getattr(pedido, "nombre_cliente", None))
    email  = _safe(getattr(pedido, "email", None))
    tel    = _safe(getattr(pedido, "telefono", None))
    ciudad = _safe(getattr(pedido, "ciudad", None))
    direccion = _safe(getattr(pedido, "direccion", None))
    metodo_entrega = _safe(getattr(pedido, "metodo_entrega", None)).replace("_", " ")
    metodo_pago = _safe(getattr(pedido, "metodo_pago", None)).replace("_", " ")
    estado = _safe(getattr(pedido, "estado", "pendiente"))
    punto_venta = _safe(getattr(pedido, "punto_venta", None))
    order_id = _build_order_id(pedido)

    # Calcular total y cantidad total desde el carrito
    total = 0.0
    total_cantidad = 0
    for it in cart_items:
        qty = int(it.get("cantidad", 1))
        unit = float(it.get("precio_unitario", 0.0))
        total += unit * qty
        total_cantidad += qty

    props: Dict[str, Any] = {
        "dealname": _compose_deal_name(pedido),
        "amount": str(total),                # numérico en string sin formateo
        "order_id": order_id,                # idempotencia
        "pipeline": HS_DEAL_PIPELINE_ID,     # FIX: propiedad correcta
        "dealstage": HS_DEAL_STAGE_ID,

        # Propiedades personalizadas Cassany (ajusta a tu portal)
        "custom_cas_numero_confirmacion": order_id,
        "custom_cas_estado_pedido": estado,
        "custom_cas_metodo_pago": metodo_pago,
        "custom_cas_metodo_entrega": metodo_entrega,
        "custom_cas_estado_pago": "pendiente",
        "custom_cas_canal_venta": "WhatsApp",
        "custom_cas_subtotal": str(total),
        "custom_cas_cantidad": str(total_cantidad if total_cantidad > 0 else (getattr(pedido, "cantidad", 1) or 1)),
        "custom_cas_telefono_cliente": tel,
        "custom_cas_nombre_cliente": nombre,
        "custom_cas_email_cliente": email,
        "custom_cas_direccion_envio": direccion,
        "custom_cas_ciudad_envio": ciudad,
        "custom_cas_notas": _safe(getattr(pedido, "notas", None)),
    }

    # Punto de venta solo si aplica
    if metodo_entrega.lower().replace(" ", "_") == "recoger_en_tienda" and punto_venta:
        props["custom_cas_punto_venta"] = punto_venta

    # Información de productos (JSON)
    if cart_items:
        primer_producto = cart_items[0]
        props.update({
            "custom_cas_sku_principal": primer_producto.get("sku", ""),
            "custom_cas_talla_principal": primer_producto.get("talla", ""),
            "custom_cas_productos": json.dumps([
                {
                    "nombre": item.get("nombre", ""),
                    "sku": item.get("sku", ""),
                    "talla": item.get("talla", ""),
                    "cantidad": item.get("cantidad", 1),
                    "precio": item.get("precio_unitario", 0)
                } for item in cart_items
            ], ensure_ascii=False)
        })

    # Asignar owner si está configurado
    if HS_DEFAULT_OWNER_ID:
        props["hubspot_owner_id"] = HS_DEFAULT_OWNER_ID

    # Limpiar propiedades vacías
    cleaned = {k: v for k, v in props.items() if v not in (None, "", " ")}
    return cleaned

# ---------- API principal ----------

def enviar_pedido_a_hubspot(pedido) -> bool:
    """
    1) Validaciones de config (token, pipeline, stage).
    2) Upsert de Contacto (email/teléfono).
    3) Idempotencia: buscar Deal con order_id (numero_confirmacion o hash de sesión).
    4) Crear Deal (pedido) con todas las propiedades.
    5) Asociar Deal ↔ Contact.
    6) (Opcional) Crear Task operativa SLA +2h y asociarla al Deal y al Contact.
    """
    # Validaciones
    if not HUBSPOT_TOKEN:
        print("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
        return False
    if not HS_DEAL_PIPELINE_ID:
        print("❗ Config inválida: HS_DEAL_PIPELINE_ID vacío o no configurado")
        return False
    if not HS_DEAL_STAGE_ID:
        print("❗ Config inválida: HS_DEAL_STAGE_ID vacío o no configurado")
        return False

    # 1) Contacto
    contact_id = _upsert_contact_and_get_id(pedido)
    if not contact_id:
        print("⚠️ No se obtuvo contact_id; continúo con Deal sin asociación.")

    # 2) Idempotencia por order_id
    order_id = _build_order_id(pedido)
    existing_deal_id = _find_deal_by_order_id(order_id)
    if existing_deal_id:
        # Asegura asociación con Contacto
        if contact_id:
            try:
                _associate_objects("deals", existing_deal_id, "contacts", contact_id)
            except Exception:
                pass
        print(f"ℹ️ Deal ya existía para order_id={order_id} (id={existing_deal_id})")
        return True

    # 3) Cargar carrito
    try:
        cart_items = json.loads(getattr(pedido, "carrito_json", "[]") or "[]")
        if not isinstance(cart_items, list):
            cart_items = []
    except Exception:
        cart_items = []

    # 4) Crear Deal
    deal_props = _prepare_deal_properties(pedido, cart_items)
    deal_id = _create_deal(deal_props)
    if not deal_id:
        return False

    # 5) Asociar Deal ↔ Contact
    if contact_id:
        try:
            _associate_objects("deals", deal_id, "contacts", contact_id)
        except Exception:
            pass

    print(f"✅ HubSpot DEAL creado id={deal_id} order_id={order_id}")

    # 6) (Opcional) Task operativa SLA +2h
    if HS_CREATE_SLA_TASK:
        task_props: Dict[str, Any] = {
            "hs_task_subject": f"Despachar {deal_props.get('dealname', 'Pedido')}",
            "hs_task_body": "Revisar pedido y contactar al cliente antes de despachar.",
            "hs_task_due_date": int(time.time() * 1000) + 2 * 60 * 60 * 1000,  # +2h epoch ms
        }
        if HS_DEFAULT_OWNER_ID:
            task_props["hubspot_owner_id"] = HS_DEFAULT_OWNER_ID

        task_id = _create_task(task_props)
        if task_id:
            # Asociar Task con Deal y Contact
            try:
                _associate_objects("tasks", task_id, "deals", deal_id)
            except Exception:
                pass
            if contact_id:
                try:
                    _associate_objects("tasks", task_id, "contacts", contact_id)
                except Exception:
                    pass
            print(f"🗓️ Task SLA creada id={task_id} asociada a deal={deal_id}")
        else:
            print("⚠️ No se pudo crear la Task SLA (opcional).")

    return True
