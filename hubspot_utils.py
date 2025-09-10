# hubspot_utils.py — v5.1 (Tasks only: crear/actualizar UNA Tarea con todo el pedido)
import os
import re
import time
import json
import hashlib
import requests
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ========= Config =========
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip()
HS_DEFAULT_OWNER_ID = os.getenv("HS_DEFAULT_OWNER_ID", "").strip()
HS_TASK_ASSOC_CONTACTS = os.getenv("HS_TASK_ASSOC_CONTACTS", "1").strip() == "1"
HS_UPSERT_CONTACTS = os.getenv("HS_UPSERT_CONTACTS", "0").strip() == "1"
HS_TASK_DUE_HOURS = int(os.getenv("HS_TASK_DUE_HOURS", "2"))  # vencimiento por defecto: ahora + 2h
ASSOC_OBJECTS_DEFAULT = "https://api.hubapi.com/crm/v4/objects/{from_obj}/{from_id}/associations/default/{to_obj}/{to_id}"


# Endpoints
BASE_CONTACTS   = "https://api.hubapi.com/crm/v3/objects/contacts"
SEARCH_CONTACTS = "https://api.hubapi.com/crm/v3/objects/contacts/search"
BASE_TASKS      = "https://api.hubapi.com/crm/v3/objects/tasks"
SEARCH_TASKS    = "https://api.hubapi.com/crm/v3/objects/tasks/search"

# Associations v4
ASSOC_LABELS  = "https://api.hubapi.com/crm/v4/associations/{from_obj}/{to_obj}/labels"
ASSOC_OBJECTS = "https://api.hubapi.com/crm/v4/objects/{from_obj}/{from_id}/associations/{to_obj}/{to_id}"

# Caches simples
_ASSOC_TYPE_CACHE: Dict[str, int] = {}

# ---------- Helpers HTTP ----------
def _headers() -> Dict[str, str]:
    if not HUBSPOT_TOKEN:
        raise RuntimeError("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }

def _request_with_retry(method: str, url: str, *, json_payload=None, params=None,
                        headers=None, timeout=15, retries=3, backoff=0.6):
    """
    Wrapper con reintentos para 429/5xx.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, headers=headers or _headers(),
                                 json=json_payload, params=params, timeout=timeout)
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

# ---------- Normalizadores / utilidades ----------
def _safe(s: Optional[str]) -> str:
    return (s or "").strip()

NUM_RE = re.compile(r"\d+")

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

    if digits.startswith("57") and len(digits) >= 12:
        base10 = digits[-10:]
        return f"57{base10}", f"+57{base10}"

    if digits.startswith(("3", "0")) and len(digits) >= 10:
        base10 = digits[-10:]
        return f"57{base10}", f"+57{base10}"

    if digits:
        return digits, f"+{digits}"
    return "", ""

def _to_e164_tel(raw: str) -> str:
    if not raw:
        return ""
    s = str(raw).strip().replace(" ", "").replace("-", "")
    if s.startswith("+"):
        return s
    digits = re.sub(r"\D", "", s)
    if digits.startswith("57") and len(digits) >= 12:
        return f"+{digits}"
    if digits.startswith(("3", "0")) and len(digits) >= 10:
        base10 = digits[-10:]
        return f"+57{base10}"
    return s

def _fmt_money_cop(v) -> str:
    try:
        return f"${float(v):,.0f}".replace(",", ".")
    except Exception:
        return "$0"

def _build_order_id(pedido) -> str:
    """
    Prefiere numero_confirmacion; si no, usa hash de session_id.
    """
    num = _safe(getattr(pedido, "numero_confirmacion", None))
    if num:
        return num
    sid = _safe(getattr(pedido, "session_id", None))
    basis = sid or f"anon-{int(time.time())}"
    return "SID-" + hashlib.md5(basis.encode("utf-8")).hexdigest()[:10]

def _compose_task_subject(order_id: str) -> str:
    return f"Pedido {order_id} — Atención humana"

def _carrito_desde_pedido(pedido) -> List[dict]:
    """
    Extrae items desde pedido.carrito_json (si existe).
    Fallback a producto/cantidad/talla/precio_unitario si no hay carrito.
    """
    items: List[dict] = []
    try:
        raw = getattr(pedido, "carrito_json", "[]") or "[]"
        data = json.loads(raw) if isinstance(raw, str) else (raw or [])
        if isinstance(data, list) and data:
            for it in data:
                nombre = it.get("nombre") or getattr(pedido, "producto", "") or "Producto"
                sku = it.get("sku") or it.get("url") or nombre
                size = it.get("talla")
                qty  = int(it.get("cantidad", 1) or 1)
                unit = float(it.get("precio_unitario", 0.0) or 0.0)
                items.append({
                    "product_name": nombre,
                    "sku": sku,
                    "size": size,
                    "quantity": qty,
                    "unit_price": unit,
                    "subtotal": unit * qty,
                    "categoria": it.get("categoria"),
                    "color": it.get("color"),
                })
    except Exception:
        items = []

    if not items:
        # Fallback si no hay carrito
        nombre = getattr(pedido, "producto", "") or "Producto"
        qty  = int(getattr(pedido, "cantidad", 0) or 0)
        unit = float(getattr(pedido, "precio_unitario", 0.0) or 0.0)
        if qty <= 0:
            qty = 1 if unit > 0 else 0
        if qty > 0 or nombre or unit > 0:
            items.append({
                "product_name": nombre,
                "sku": nombre,
                "size": getattr(pedido, "talla", None),
                "quantity": qty,
                "unit_price": unit,
                "subtotal": unit * max(1, qty),
                "categoria": "",
                "color": None,
            })
    return items

def _build_task_body(pedido) -> str:
    order_id      = _build_order_id(pedido)
    order_status  = _safe(getattr(pedido, "estado", "")) or "Confirmado"

    # Fecha: usa last_activity si existe, si no hoy (UTC)
    try:
        dt = getattr(pedido, "last_activity", None)
        if isinstance(dt, str):
            fecha = dt.split(" ")[0]
        elif isinstance(dt, datetime):
            fecha = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        else:
            fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    source_channel = "WhatsApp"
    store          = _safe(getattr(pedido, "punto_venta", None)) or "N/A"

    customer_name  = _safe(getattr(pedido, "nombre_cliente", None)) or "[Sin nombre]"
    raw_phone      = getattr(pedido, "telefono", "") or (safe(getattr(pedido, "session_id", "")) or "").replace("cliente", "").replace("cliente", "")
    customer_phone = _to_e164_tel(raw_phone)
    customer_email = _safe(getattr(pedido, "email", None)) or "(no disponible)"
    customer_doc   = _safe(getattr(pedido, "tipo_documento", None)) or "(no disponible)"

    delivery_method  = (safe(getattr(pedido, "metodo_entrega", None)) or "por definir").replace("", " ")
    delivery_address = _safe(getattr(pedido, "direccion", None)) or "(no disponible)"
    delivery_city    = _safe(getattr(pedido, "ciudad", None)) or ""
    if delivery_city and delivery_city not in delivery_address:
        delivery_address = (delivery_address if delivery_address != "(no disponible)" else "") + (f" - {delivery_city}" if delivery_city else "")

    payment_method    = (safe(getattr(pedido, "metodo_pago", None)) or "por definir").replace("", " ").capitalize()
    payment_status    = _safe(getattr(pedido, "estado_pago", None)) or "pendiente"
    payment_reference = _safe(getattr(pedido, "referencia_pago", None)) or "(no disponible)"

    notes = _safe(getattr(pedido, "notas", None)) or "Ninguna"

    items = _carrito_desde_pedido(pedido)
    total = sum(float(i.get("subtotal", 0.0) or 0.0) for i in items)
    subtotal_db = getattr(pedido, "subtotal", None)
    if subtotal_db not in (None, "", " "):
        try:
            total = float(subtotal_db)
        except Exception:
            pass

    lines: List[str] = []
    P = lines.append

    P("📦 Pedido para atención humana\n")
    P(f"Número de confirmación: {order_id}")
    P(f"Estado del pedido: {order_status}")
    P(f"Fecha: {fecha}")
    P(f"Canal de venta: {source_channel}")
    P(f"Punto de venta: {store}\n")

    P("👤 Cliente")
    P(f"- Nombre: {customer_name}")
    P(f"- Teléfono: {customer_phone or '(no disponible)'}")
    P(f"- Correo: {customer_email}")
    P(f"- Documento: {customer_doc}\n")

    P("🛍️ Productos")
    if items:
        for it in items:
            p_name = _safe(it.get("product_name")) or "Producto"
            sku    = _safe(it.get("sku"))
            size   = _safe(it.get("size")) or "por definir"
            qty    = int(it.get("quantity", 1) or 1)
            unit   = float(it.get("unit_price", 0.0) or 0.0)
            sub    = float(it.get("subtotal", unit * qty) or 0.0)
            P(f"- {p_name}" + (f" (SKU {sku})" if sku else ""))
            P(f"  Talla: {size} | Cantidad: {qty} | Precio unitario: {_fmt_money_cop(unit)} | Subtotal: {_fmt_money_cop(sub)}")
    else:
        P("- (sin ítems)")

    P("")  # línea en blanco

    P("💳 Pago")
    P(f"- Método: {payment_method}")
    P(f"- Estado: {payment_status}")
    P(f"- Referencia/Comprobante: {payment_reference}\n")

    P("🚚 Entrega")
    P(f"- Método: {delivery_method}")
    P(f"- Dirección: {delivery_address or '(no disponible)'}")
    P(f"- Teléfono entrega: {customer_phone or '(no disponible)'}\n")

    P(f"💵 Total estimado: {_fmt_money_cop(total)}")
    P(f"🗒️ Notas: {notes}\n")

    return "\n".join(lines)

# ---------- Contactos (buscar / upsert opcional) ----------
def _build_search_body(email: str, phone_variants: Tuple[str, str]) -> Dict[str, Any]:
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
        "filterGroups": groups,
        "properties": ["email", "firstname", "phone", "mobilephone"],
        "limit": 1,
    }

def _search_contact(email: str, phone_variants: Tuple[str, str]) -> Optional[str]:
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

def _build_email_from_phone(session_id: Optional[str], telefono: Optional[str]) -> str:
    telefono = _safe(telefono)
    plain57, _ = _norm_phone_variants(telefono)
    if plain57:
        return f"{plain57}@cassany.co"
    sid = _safe(session_id)
    if sid:
        h = hashlib.sha1(sid.encode("utf-8")).hexdigest()[:7]
        return f"sid_{h}@cassany.co"
    return f"guest_{int(time.time())}@cassany.co"

def _prepare_contact_properties(pedido) -> Dict[str, Any]:
    telefono_raw = getattr(pedido, "telefono", None) or safe(getattr(pedido, "session_id", "")).replace("cliente", "").replace("cliente", "")
    plain57, plus57 = _norm_phone_variants(telefono_raw)
    email_real = _safe(getattr(pedido, "email", ""))
    email = email_real or _build_email_from_phone(getattr(pedido, "session_id", None), telefono_raw)

    nombre    = _safe(getattr(pedido, "nombre_cliente", None)) or "Cliente"
    direccion = _safe(getattr(pedido, "direccion", None))
    ciudad    = _safe(getattr(pedido, "ciudad", None))

    props: Dict[str, Any] = {
        "email": email,
        "firstname": nombre,
        "phone": plus57 or plain57,
        "mobilephone": plus57 or plain57,
        "address": direccion,
        "city": ciudad,
    }
    return {k: v for k, v in props.items() if v not in (None, "", " ")}

def _upsert_contact_and_get_id(pedido) -> Optional[str]:
    """
    Si HS_UPSERT_CONTACTS=1: upsert contacto y retorna ID.
    Si HS_UPSERT_CONTACTS=0: solo busca y retorna ID si existe; no crea.
    """
    if not HUBSPOT_TOKEN:
        print("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
        return None

    props = _prepare_contact_properties(pedido)
    email = props.get("email", "")
    telefono_raw = getattr(pedido, "telefono", None) or safe(getattr(pedido, "session_id", "")).replace("cliente", "").replace("cliente", "")
    phone_variants = _norm_phone_variants(telefono_raw)

    # Intentar encontrar primero
    contact_id = _search_contact(email=email, phone_variants=phone_variants)
    if contact_id:
        if HS_UPSERT_CONTACTS:
            try:
                url = f"{BASE_CONTACTS}/{contact_id}"
                r = _request_with_retry("PATCH", url, json_payload={"properties": props}, timeout=10)
                r.raise_for_status()
                print(f"✅ HubSpot UPDATE OK (contact {contact_id})")
            except Exception as e:
                print("⚠️ No se pudo actualizar contacto existente:", repr(e))
        return contact_id

    if not HS_UPSERT_CONTACTS:
        # No crear contacto si la bandera lo impide
        return None

    # Crear
    try:
        r = _request_with_retry("POST", BASE_CONTACTS, json_payload={"properties": props}, timeout=10)
        r.raise_for_status()
        cid = (r.json() or {}).get("id")
        print(f"✅ HubSpot CREATE OK (contact {cid})")
        return cid
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot create contact error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot create contact error:", repr(e))
        except Exception:
            print("❌ HubSpot create contact error:", repr(e))
    return None

# ---------- Associations (para task->contact) ----------
def _get_assoc_type_id(from_obj: str, to_obj: str) -> int:
    key = f"{from_obj}->{to_obj}"
    if key in _ASSOC_TYPE_CACHE:
        return _ASSOC_TYPE_CACHE[key]
    url = ASSOC_LABELS.format(from_obj=from_obj, to_obj=to_obj)
    r = _request_with_retry("GET", url, timeout=10)
    r.raise_for_status()
    data = r.json() or {}
    results = data.get("results") or []
    for it in results:
        if it.get("category") == "HUBSPOT_DEFINED" and isinstance(it.get("typeId"), int):
            _ASSOC_TYPE_CACHE[key] = it["typeId"]
            return it["typeId"]
    if results and isinstance(results[0].get("typeId"), int):
        _ASSOC_TYPE_CACHE[key] = results[0]["typeId"]
        return results[0]["typeId"]
    raise RuntimeError(f"No se halló associationTypeId para {from_obj}->{to_obj}")

def _associate_objects(from_obj: str, from_id: str, to_obj: str, to_id: str, *, labeled: bool = False):
    """
    v4 associations:
    - Default (unlabeled): PUT .../associations/default/...   # sin body
    - Labeled:            PUT .../associations/...            # body = [ {associationCategory, associationTypeId} ]
    """
    try:
        if not labeled:
            # ✅ Opción simple: asociación por defecto, SIN body
            url = ASSOC_OBJECTS_DEFAULT.format(from_obj=from_obj, from_id=from_id, to_obj=to_obj, to_id=to_id)
            r = _request_with_retry("PUT", url, json_payload=None, timeout=10)
            r.raise_for_status()
            return

        # ✅ Opción con etiqueta (solo si realmente la necesitas)
        type_id = _get_assoc_type_id(from_obj, to_obj)
        url = ASSOC_OBJECTS.format(from_obj=from_obj, from_id=from_id, to_obj=to_obj, to_id=to_id)
        body = [
            {
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": type_id
            }
        ]
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


# ---------- Tasks (buscar / crear / actualizar) ----------
def _search_task_by_subject(subject: str) -> Optional[str]:
    """
    Busca una Tarea exacta por hs_task_subject.
    """
    body = {
        "filterGroups": [{
            "filters": [
                {"propertyName": "hs_task_subject", "operator": "EQ", "value": subject}
            ]
        }],
        "properties": ["hs_task_subject"],
        "limit": 1
    }
    try:
        r = _request_with_retry("POST", SEARCH_TASKS, json_payload=body, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results", [])
        if results:
            return results[0].get("id")
    except Exception as e:
        print("❌ HubSpot search task error:", repr(e))
    return None

def _now_epoch_ms_plus(hours: int = 0) -> int:
    return int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp() * 1000)


def _create_task(subject: str, body_text: str) -> Optional[str]:
    payload = {
        "properties": {
            "hs_task_subject": subject,
            "hs_task_body": body_text,
            "hs_task_status": "NOT_STARTED",
            "hs_task_priority": "HIGH",
            "hs_timestamp": _now_epoch_ms_plus(HS_TASK_DUE_HOURS),
        }
    }
    if HS_DEFAULT_OWNER_ID:
        payload["properties"]["hubspot_owner_id"] = HS_DEFAULT_OWNER_ID
    try:
        r = _request_with_retry("POST", BASE_TASKS, json_payload=payload, timeout=15)
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

def _update_task(task_id: str, body_text: str) -> bool:
    payload = {"properties": {"hs_task_body": body_text, "hs_task_priority": "HIGH"}}
    try:
        r = _request_with_retry("PATCH", f"{BASE_TASKS}/{task_id}", json_payload=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        try:
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print("❌ HubSpot update task error:", e, e.response.status_code, e.response.text)
            else:
                print("❌ HubSpot update task error:", repr(e))
        except Exception:
            print("❌ HubSpot update task error:", repr(e))
    return False

# ---------- API principal ----------
def enviar_pedido_a_hubspot(pedido) -> bool:
    """
    Crea o actualiza UNA Tarea con todo el pedido en el body.
    Idempotencia por subject = "Pedido {order_id} — Atención humana".
    (Opcional) asocia la tarea al contacto (buscado o upsert, según flags).
    """
    try:
        if not HUBSPOT_TOKEN:
            print("❗ Falta HUBSPOT_ACCESS_TOKEN en .env")
            return False

        # Construir subject/body
        order_id = _build_order_id(pedido)
        subject  = _compose_task_subject(order_id)
        body_txt = _build_task_body(pedido)

        # Buscar si ya existe la task
        task_id = _search_task_by_subject(subject)

        if task_id:
            ok = _update_task(task_id, body_txt)
            if not ok:
                return False
            print(f"ℹ️ Task actualizada (id={task_id}) subject='{subject}'")
        else:
            task_id = _create_task(subject, body_txt)
            if not task_id:
                return False
            print(f"✅ Task creada (id={task_id}) subject='{subject}'")

        # (Opcional) asociación a contacto
        if HS_TASK_ASSOC_CONTACTS:
            contact_id = None
            if HS_UPSERT_CONTACTS:
                contact_id = _upsert_contact_and_get_id(pedido)
            else:
                # Solo buscar, NO crear
                telefono_raw = getattr(pedido, "telefono", None) or safe(getattr(pedido, "session_id", "")).replace("cliente", "").replace("cliente", "")
                email_guess  = _build_email_from_phone(getattr(pedido, "session_id", None), telefono_raw)
                contact_id = _search_contact(email_guess, _norm_phone_variants(telefono_raw))
            if contact_id:
                try:
                    _associate_objects("tasks", task_id, "contacts", contact_id)
                except Exception:
                    pass

        return True

    except Exception as e:
        print("❌ Error enviar_pedido_a_hubspot:", repr(e))
        return False
