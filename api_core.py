# api_core.py
# Motor conversacional + endpoint /mensaje-whatsapp (APIRouter)
# Requiere módulos locales: carrito.py, filtros.py
# y los que ya tienes en tu repo: crud.py, database.py, models.py, hubspot_utils.py,
# utils_intencion.py, utils_mensaje_whatsapp.py, woocommerce_gpt_utils.py

import os
import re
import json
import hmac
import hashlib
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Literal

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from openai import OpenAI

from database import SessionLocal
from models import Pedido
from crud import (
    actualizar_pedido_por_sesion,
    crear_pedido,
    obtener_pedido_por_sesion,
)
from hubspot_utils import enviar_pedido_a_hubspot
from utils_intencion import detectar_intencion_atencion
from utils_mensaje_whatsapp import generar_mensaje_atencion_humana
from woocommerce_gpt_utils import sugerir_productos, detectar_categoria, detectar_atributos

# módulos locales nuevos
from carrito import (
    fmt_cop,
    carrito_load,
    carrito_save,
    cart_add,
    cart_update_qty,
    cart_remove,
    cart_total,
    cart_summary_lines,
)
from filtros import (
    SALUDO_RE, MAS_OPCIONES_RE, DOMICILIO_RE, RECOGER_RE, SELECCION_RE,
    ADD_RE, OFFTOPIC_RE, SMALLTALK_RE, DISCOVERY_RE, CARRO_RE, MOSTRAR_RE, FOTOS_RE,
    TALLA_RE, NOMBRE_RE, TALLA_TOKEN_RE, USO_RE, MANGA_RE, COLOR_RE,
    ORDINALES_MAP, ORDINAL_RE,
    _norm_txt, extract_qty
)

# -------------------------------------------------------------------
# Router y runtime
# -------------------------------------------------------------------
router = APIRouter()
client: Optional[OpenAI] = None

# -------------------------------------------------------------------
# ENV y constantes
# -------------------------------------------------------------------
load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT_ID")

WA_GRAPH_API_VER = os.getenv("WA_GRAPH_API_VER", "v17.0")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WA_APP_SECRET = os.getenv("WA_APP_SECRET", "")

ALERTA_WHATSAPP = os.getenv("ALERTA_WHATSAPP", "+573113305646")
CTA_BANCOLOMBIA = os.getenv("CTA_BANCOLOMBIA", "27480228756")
CTA_DAVIVIENDA = os.getenv("CTA_DAVIVIENDA", "037169997501")

HUBSPOT_TOKEN_PRESENT = bool(os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip())
print(f"🔧 HubSpot token presente: {HUBSPOT_TOKEN_PRESENT}")

SUPPORT_START_HOUR, SUPPORT_END_HOUR = 9, 19
UTC = timezone.utc

PUNTOS_VENTA = [
    "C.C Premium Plaza",
    "C.C Mayorca",
    "C.C Unicentro",
    "Centro - Colombia",
    "C.C La Central",
    "Centro - Junín",
    "C.C Florida",
]

CATEGORIAS_RESUMEN = [
    "camisas (incluye guayaberas)", "jeans", "pantalones",
    "bermudas", "blazers", "suéteres", "camisetas", "calzado", "accesorios"
]

# -------------------------------------------------------------------
# OpenAI client init

import os
import re
import json
import hmac
import hashlib
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Literal

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from openai import OpenAI

from database import SessionLocal
from models import Pedido
from crud import (
    actualizar_pedido_por_sesion,
    crear_pedido,
    obtener_pedido_por_sesion,
)
from hubspot_utils import enviar_pedido_a_hubspot
from utils_intencion import detectar_intencion_atencion
from utils_mensaje_whatsapp import generar_mensaje_atencion_humana
from woocommerce_gpt_utils import sugerir_productos, detectar_categoria, detectar_atributos

# módulos locales nuevos
from carrito import (
    fmt_cop,
    carrito_load,
    carrito_save,
    cart_add,
    cart_update_qty,
    cart_remove,
    cart_total,
    cart_summary_lines,
)
from filtros import (
    SALUDO_RE, MAS_OPCIONES_RE, DOMICILIO_RE, RECOGER_RE, SELECCION_RE,
    ADD_RE, OFFTOPIC_RE, SMALLTALK_RE, DISCOVERY_RE, CARRO_RE, MOSTRAR_RE, FOTOS_RE,
    TALLA_RE, NOMBRE_RE, TALLA_TOKEN_RE, USO_RE, MANGA_RE, COLOR_RE,
    ORDINALES_MAP, ORDINAL_RE,
    _norm_txt, extract_qty
)

# -------------------------------------------------------------------
# Router y runtime
# -------------------------------------------------------------------
router = APIRouter()
client: Optional[OpenAI] = None

# -------------------------------------------------------------------
# ENV y constantes
# -------------------------------------------------------------------
load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT_ID")

WA_GRAPH_API_VER = os.getenv("WA_GRAPH_API_VER", "v17.0")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WA_APP_SECRET = os.getenv("WA_APP_SECRET", "")

ALERTA_WHATSAPP = os.getenv("ALERTA_WHATSAPP", "+573113305646")
CTA_BANCOLOMBIA = os.getenv("CTA_BANCOLOMBIA", "27480228756")
CTA_DAVIVIENDA = os.getenv("CTA_DAVIVIENDA", "037169997501")

HUBSPOT_TOKEN_PRESENT = bool(os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip())
print(f"🔧 HubSpot token presente: {HUBSPOT_TOKEN_PRESENT}")

SUPPORT_START_HOUR, SUPPORT_END_HOUR = 9, 19
UTC = timezone.utc

PUNTOS_VENTA = [
    "C.C Premium Plaza",
    "C.C Mayorca",
    "C.C Unicentro",
    "Centro - Colombia",
    "C.C La Central",
    "Centro - Junín",
    "C.C Florida",
]

CATEGORIAS_RESUMEN = [
    "camisas (incluye guayaberas)", "jeans", "pantalones",
    "bermudas", "blazers", "suéteres", "camisetas", "calzado", "accesorios"
]

# -------------------------------------------------------------------
# OpenAI client init

import os
import re
import json
import hmac
import hashlib
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Literal

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from openai import OpenAI

from database import SessionLocal
from models import Pedido
from crud import (
    actualizar_pedido_por_sesion,
    crear_pedido,
    obtener_pedido_por_sesion,
)
from hubspot_utils import enviar_pedido_a_hubspot
from utils_intencion import detectar_intencion_atencion
from utils_mensaje_whatsapp import generar_mensaje_atencion_humana
from woocommerce_gpt_utils import sugerir_productos, detectar_categoria, detectar_atributos

# módulos locales nuevos
from carrito import (
    fmt_cop,
    carrito_load,
    carrito_save,
    cart_add,
    cart_update_qty,
    cart_remove,
    cart_total,
    cart_summary_lines,
)
from filtros import (
    SALUDO_RE, MAS_OPCIONES_RE, DOMICILIO_RE, RECOGER_RE, SELECCION_RE,
    ADD_RE, OFFTOPIC_RE, SMALLTALK_RE, DISCOVERY_RE, CARRO_RE, MOSTRAR_RE, FOTOS_RE,
    TALLA_RE, NOMBRE_RE, TALLA_TOKEN_RE, USO_RE, MANGA_RE, COLOR_RE,
    ORDINALES_MAP, ORDINAL_RE,
    _norm_txt, extract_qty
)

# -------------------------------------------------------------------
# Router y runtime
# -------------------------------------------------------------------
router = APIRouter()
client: Optional[OpenAI] = None

# -------------------------------------------------------------------
# ENV y constantes
# -------------------------------------------------------------------
load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT_ID")

WA_GRAPH_API_VER = os.getenv("WA_GRAPH_API_VER", "v17.0")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WA_APP_SECRET = os.getenv("WA_APP_SECRET", "")

ALERTA_WHATSAPP = os.getenv("ALERTA_WHATSAPP", "+573113305646")
CTA_BANCOLOMBIA = os.getenv("CTA_BANCOLOMBIA", "27480228756")
CTA_DAVIVIENDA = os.getenv("CTA_DAVIVIENDA", "037169997501")

HUBSPOT_TOKEN_PRESENT = bool(os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip())
print(f"🔧 HubSpot token presente: {HUBSPOT_TOKEN_PRESENT}")

SUPPORT_START_HOUR, SUPPORT_END_HOUR = 9, 19
UTC = timezone.utc

PUNTOS_VENTA = [
    "C.C Premium Plaza",
    "C.C Mayorca",
    "C.C Unicentro",
    "Centro - Colombia",
    "C.C La Central",
    "Centro - Junín",
    "C.C Florida",
]

CATEGORIAS_RESUMEN = [
    "camisas (incluye guayaberas)", "jeans", "pantalones",
    "bermudas", "blazers", "suéteres", "camisetas", "calzado", "accesorios"
]

# -------------------------------------------------------------------
# OpenAI client init
# -------------------------------------------------------------------
def init_runtime():
    """Llamado desde main.py al arrancar la app."""
    global client
    if OPENAI_API_KEY:
        kwargs = {"api_key": OPENAI_API_KEY, "timeout": 30, "max_retries": 2}
        if OPENAI_PROJECT_ID:
            kwargs["project"] = OPENAI_PROJECT_ID
        client = OpenAI(**kwargs)
    else:
        client = None
        print("⚠️  OPENAI_API_KEY no definido. Arranca sin LLM; endpoints seguirán respondiendo.")

# -------------------------------------------------------------------
# WhatsApp helpers (expuestos también para webhook.py)
# -------------------------------------------------------------------
def _normalize_to_msisdn(numero: str) -> str:
    return "".join(ch for ch in str(numero) if ch.isdigit())

async def enviar_mensaje_whatsapp(numero: str, mensaje: str):
    url = f"https://graph.facebook.com/{WA_GRAPH_API_VER}/{WHATSAPP_PHONE_NUMBER}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    to_msisdn = _normalize_to_msisdn(numero)
    payload = {
        "messaging_product": "whatsapp",
        "to": to_msisdn,
        "type": "text",
        "text": {"body": mensaje}
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client_http:
            r = await client_http.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            print("❌ Error envío WhatsApp:", r.status_code, r.text)
            r.raise_for_status()
        print(f"✅ Mensaje enviado a {to_msisdn}")
    except Exception as exc:
        print("❌ Error envío WhatsApp (exception):", repr(exc))
        print("📤 Endpoint:", url)
        print("📨 Payload:", payload)

# -------------------------------------------------------------------
# Modelos / dependencia DB
# -------------------------------------------------------------------
class UserMessage(BaseModel):
    message: str

class PedidoEntrada(BaseModel):
    session_id: str
    producto: str
    cantidad: int = 1
    talla: str = ""
    precio_unitario: float = 0.0
    nombre_completo: str
    telefono: str
    email: EmailStr
    metodo_entrega: Literal["domicilio", "recoger_en_tienda"]
    direccion: Optional[str] = None
    ciudad: Optional[str] = None
    punto_venta: Optional[str] = None
    metodo_pago: Literal["transferencia", "payu", "pago_en_tienda"]
    notas: Optional[str] = ""

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -------------------------------------------------------------------
# Helpers de estado/JSON/persistencia
# -------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(UTC)

def _safe_json_load(s: str, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

def _get_saludo_enviado(db: Session, session_id: str) -> int:
    try:
        row = db.execute(sa_text("SELECT saludo_enviado FROM pedidos WHERE session_id=:sid"), {"sid": session_id}).fetchone()
        return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0

def _get_last_msg_id(db: Session, session_id: str) -> Optional[str]:
    try:
        row = db.execute(sa_text("SELECT last_msg_id FROM pedidos WHERE session_id=:sid"), {"sid": session_id}).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None

def set_user_filter(db: Session, session_id: str, filtro: dict):
    try:
        db.execute(sa_text("UPDATE pedidos SET filtros = :f WHERE session_id = :sid"),
                   {"f": json.dumps(filtro, ensure_ascii=False), "sid": session_id})
        db.commit()
    except Exception:
        db.rollback()

def get_user_filter(db: Session, session_id: str) -> Optional[dict]:
    try:
        row = db.execute(sa_text("SELECT filtros FROM pedidos WHERE session_id = :sid"), {"sid": session_id}).fetchone()
        return json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None

def _get_sugeridos_urls(db: Session, session_id: str) -> List[str]:
    try:
        row = db.execute(sa_text("SELECT sugeridos FROM pedidos WHERE session_id=:sid"), {"sid": session_id}).fetchone()
        txt = row[0] if row and row[0] else ""
        if txt.strip().startswith("["):
            try:
                arr = json.loads(txt)
                return [u for u in arr if isinstance(u, str) and u.startswith("http")]
            except Exception:
                pass
        return [u for u in txt.split() if u.startswith("http")]
    except Exception:
        return []

def _append_sugeridos_urls(db: Session, session_id: str, nuevos: List[str]):
    prev = _get_sugeridos_urls(db, session_id)
    combined = list(dict.fromkeys(prev + nuevos))
    actualizar_pedido_por_sesion(db, session_id, "sugeridos", " ".join(combined))

def _set_sugeridos_list(db: Session, session_id: str, lista: List[dict]):
    try:
        db.execute(sa_text("UPDATE pedidos SET sugeridos_json=:j WHERE session_id=:sid"),
                   {"j": json.dumps(lista, ensure_ascii=False), "sid": session_id})
        db.commit()
    except Exception:
        db.rollback()

def _get_sugeridos_list(db: Session, session_id: str) -> List[dict]:
    try:
        row = db.execute(sa_text("SELECT sugeridos_json FROM pedidos WHERE session_id=:sid"), {"sid": session_id}).fetchone()
    except Exception:
        return []
    try:
        return json.loads(row[0]) if row and row[0] else []
    except Exception:
        return []

def _get_ultima_cat_filters(db: Session, session_id: str):
    try:
        row = db.execute(sa_text("SELECT ultima_categoria, ultimos_filtros FROM pedidos WHERE session_id=:sid"),
                         {"sid": session_id}).fetchone()
        ultima_cat = row[0] if row and row[0] else None
        try:
            ult_filtros = json.loads(row[1]) if row and row[1] else {}
        except Exception:
            ult_filtros = {}
        return ultima_cat, ult_filtros
    except Exception:
        return None, {}

def _ctx_load(pedido) -> dict:
    try:
        sid = pedido.session_id
        db = SessionLocal()
        try:
            row = db.execute(sa_text("SELECT ctx_json FROM pedidos WHERE session_id=:sid"), {"sid": sid}).fetchone()
        finally:
            db.close()
        raw = row[0] if row and row[0] else "{}"
        return _safe_json_load(raw, {})
    except Exception:
        return {}

def _ctx_save(db: Session, session_id: str, ctx: dict):
    try:
        db.execute(sa_text("UPDATE pedidos SET ctx_json=:j WHERE session_id=:sid"),
                   {"j": json.dumps(ctx, ensure_ascii=False), "sid": session_id})
        db.commit()
    except Exception:
        db.rollback()

def _remember_list(db: Session, session_id: str, cat: str, filtros: dict, productos: List[dict]):
    try:
        db.execute(
            sa_text("UPDATE pedidos SET ultima_categoria=:c, ultimos_filtros=:f, sugeridos_json=:s WHERE session_id=:sid"),
            {"c": cat or "", "f": json.dumps(filtros, ensure_ascii=False),
             "s": json.dumps(productos, ensure_ascii=False), "sid": session_id}
        )
        db.commit()
    except Exception:
        db.rollback()

    pedido = obtener_pedido_por_sesion(db, session_id)
    ctx = _ctx_load(pedido)
    ctx["ultima_categoria"] = cat
    ctx["ultimos_filtros"] = filtros
    ctx["ultima_lista"] = productos
    _ctx_save(db, session_id, ctx)

def _remember_selection(db: Session, session_id: str, prod: dict, idx: int):
    pedido = obtener_pedido_por_sesion(db, session_id)
    ctx = _ctx_load(pedido)
    sel = {
        "idx": idx,
        "nombre": prod.get("nombre"),
        "url": prod.get("url"),
        "precio": prod.get("precio"),
        "talla": None,
        "cantidad": None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    ctx.setdefault("selecciones", []).append(sel)
    db2 = SessionLocal()
    try:
        _ctx_save(db2, session_id, ctx)
    finally:
        db2.close()

def _update_last_selection_from_pedido(db: Session, session_id: str):
    pedido = obtener_pedido_por_sesion(db, session_id)
    ctx = _ctx_load(pedido)
    if not ctx.get("selecciones"):
        return
    last = ctx["selecciones"][-1]
    if getattr(pedido, "talla", None):
        last["talla"] = pedido.talla
    if getattr(pedido, "cantidad", None):
        last["cantidad"] = pedido.cantidad
    _ctx_save(db, session_id, ctx)

# -------------------------------------------------------------------
# Clasificador pago/confirmación (LLM)
# -------------------------------------------------------------------
async def detectar_intencion_pago_confirmacion(texto: str) -> dict:
    if client is None:
        return {"intent": "ninguno", "method": None, "confidence": 0.0}
    try:
        schema_msg = (
            "Clasifica la intención del usuario respecto al flujo de compra.\n"
            "Responde SOLO JSON con estas claves:\n"
            "{\n"
            '  "intent": "pago" | "confirmar" | "ninguno",\n'
            '  "method": "transferencia" | "payu" | "pago_en_tienda" | null,\n'
            '  "confidence": number\n'
            "}\n\n"
            "Mapeo: transferencia/bancolombia/davivienda -> transferencia; payu/pse -> payu; "
            "efectivo/pago en tienda/contraentrega -> pago_en_tienda"
        )
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.6,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": schema_msg},
                {"role": "user", "content": texto.strip()},
            ],
            max_tokens=350,
        )
        raw = completion.choices[0].message.content.strip()
        data = json.loads(raw)
        intent = data.get("intent") if data.get("intent") in {"pago", "confirmar", "ninguno"} else "ninguno"
        method = data.get("method") if data.get("method") in {"transferencia","payu","pago_en_tienda"} else None
        conf = float(data.get("confidence") or 0.0)
        return {"intent": intent, "method": method, "confidence": conf}
    except Exception:
        return {"intent": "ninguno", "method": None, "confidence": 0.0}

# -------------------------------------------------------------------
# Prompt base
# -------------------------------------------------------------------
try:
    with open("prompt_cassany_gpt_final.txt", "r", encoding="utf-8") as fh:
        base_prompt = fh.read().strip()
except Exception as e:
    print("⚠️  No encontré prompt_cassany_gpt_final.txt, usando prompt mínimo:", e)
    base_prompt = "Eres un asistente de ventas de CASSANY. Responde breve y profesional."

ACTIONS_PROTOCOL = """
=== PROTOCOLO DE ACCIONES (OBLIGATORIO) ===
Cuando el usuario pida operar el carrito (agregar, quitar, ver, cambiar talla), RESPONDE SOLO con JSON válido (sin texto extra) usando exactamente uno de:
{"action":"ADD_TO_CART","product_ref":"<n|id|url>","size":null,"qty":1}
{"action":"REMOVE_FROM_CART","product_id":123}
{"action":"SHOW_CART"}
{"action":"ASK_VARIANT","product_ref":"<n|id|url>","missing":"size","qty":1}
{"action":"CLARIFY","question":"¿Cuál talla prefieres?"}

Reglas:
- "product_ref": acepta el índice mostrado al usuario (1,2,3), el id del producto o su URL.
- Si el producto requiere talla y el usuario no la dio, usa ASK_VARIANT **incluyendo siempre product_ref** y opcionalmente "qty".
- Si el usuario dice “agrega el 1”, usa {"action":"ADD_TO_CART","product_ref":"1"}.
- NUNCA mezcles texto humano con el JSON; la respuesta debe ser solo el JSON.
""".strip()

# -------------------------------------------------------------------
# Faltantes (corregido para no repreguntar si ya hay carrito)
# -------------------------------------------------------------------
def _pedido_missing_fields(pedido) -> list:
    """
    Devuelve una lista de campos faltantes priorizados.
    Si ya existe carrito, NO se piden 'producto' ni 'cantidad'.
    """
    faltan = []
    carrito = carrito_load(pedido)

    pedir_nombre = bool(carrito) or bool(getattr(pedido, "metodo_entrega", "")) or bool(getattr(pedido, "metodo_pago", ""))
    if pedir_nombre and not getattr(pedido, "nombre_cliente", None):
        faltan.append("nombre_cliente")

    met_ent = (getattr(pedido, "metodo_entrega", "") or "").strip()
    if not met_ent:
        faltan.append("metodo_entrega")
    else:
        if met_ent == "domicilio":
            if not getattr(pedido, "direccion", None):
                faltan.append("direccion")
            if not getattr(pedido, "ciudad", None):
                faltan.append("ciudad")
        elif met_ent == "recoger_en_tienda":
            if not getattr(pedido, "punto_venta", None):
                faltan.append("punto_venta")

    if not carrito:
        if not getattr(pedido, "producto", None):
            faltan.append("producto")
        if (getattr(pedido, "producto", None)) and not (getattr(pedido, "cantidad", 0) or 0) and not _ctx_load(pedido).get("awaiting_qty"):
            faltan.append("cantidad")

    if not getattr(pedido, "metodo_pago", None):
        faltan.append("metodo_pago")

    return faltan

def _prompt_for_missing(pedido, faltan: list) -> str:
    if not faltan:
        return ""
    f = faltan[0]
    if f == "nombre_cliente":
        return "¿Cómo te llamas? (nombre y apellido)"
    if f == "metodo_entrega":
        return "¿Prefieres envío a domicilio o recoger en tienda?"
    if f == "direccion":
        return ("Antes de continuar, recuerda que tus datos se tratan bajo nuestra política: "
                "https://cassany.co/tratamiento-de-datos-personales/\n"
                "¿Cuál es tu dirección de envío?")
    if f == "ciudad":
        return "¿En qué ciudad se realizará el envío?"
    if f == "punto_venta":
        tiendas = "\n".join(PUNTOS_VENTA)
        return f"¿En cuál tienda deseas recoger tu pedido?\n{tiendas}"
    if f == "producto":
        cats = ", ".join(CATEGORIAS_RESUMEN[:4]) + "…"
        return f"¿Qué te gustaría ver primero? Tenemos {cats}"
    if f == "cantidad":
        return "¿Cuántas unidades deseas?"
    if f == "metodo_pago":
        return ("¿Qué método de pago prefieres?\n"
                "- Transferencia (Bancolombia/Davivienda)\n"
                "- PayU (link de pago)\n"
                "- Pago en tienda")
    return "¿Te parece si seguimos con el siguiente paso?"

# -------------------------------------------------------------------
# Conversación de pago/confirmación (router híbrido)
# -------------------------------------------------------------------
async def procesar_mensaje_usuario(text: str, db, session_id, pedido):
    pago_match = re.compile(
        r'(pagar|pago|quiero pagar|voy a pagar|prefiero pagar|el pago|pagaremos|pagare).*(transferencia|bancolombia|davivienda|pse|payu|pago en tienda|efectivo|contraentrega)'
        r'|(transferencia|bancolombia|davivienda|pse|payu|pago en tienda|efectivo|contraentrega).*(pagar|pago|quiero|voy|prefiero|pagaremos|pagare)',
        re.I
    ).search(text)
    confirm_match = re.compile(
        r"\b(confirmar|confirmo|finalizar|cerrar|terminar|realizar)\b.*\b(pedido|compra|orden)\b", re.I
    ).search(text)

    intent_det = {"intent": "ninguno", "method": None, "confidence": 0.0}
    if not (pago_match or confirm_match):
        intent_det = await detectar_intencion_pago_confirmacion(text)

    def _infer_method_from_text(t: str) -> Optional[str]:
        t = t.lower()
        if any(k in t for k in ["transferencia", "bancolombia", "davivienda"]):
            return "transferencia"
        if "payu" in t or "pse" in t:
            return "payu"
        if any(k in t for k in ["pago en tienda", "efectivo", "contraentrega", "en tienda"]):
            return "pago_en_tienda"
        return None

    if pago_match or (intent_det["intent"] == "pago" and intent_det["confidence"] >= 0.6):
        method = intent_det["method"] or _infer_method_from_text(text) or "pago_en_tienda"
        actualizar_pedido_por_sesion(db, session_id, "metodo_pago", method)

        if method == "transferencia":
            return {
                "response": (
                    "Perfecto. Realiza la transferencia y envía el comprobante por este chat:\n"
                    f"- Bancolombia: Cuenta Corriente No. {CTA_BANCOLOMBIA}\n"
                    f"- Davivienda: Cuenta Corriente No. {CTA_DAVIVIENDA}\n\n"
                    "Apenas lo recibamos, confirmamos tu pedido. ¿Deseas agregar algo más mientras tanto?"
                )
            }
        elif method == "payu":
            return {
                "response": (
                    "Perfecto. Procesaremos el pago con PayU desde nuestro sitio web. "
                    "Te compartiremos el enlace para completar el pago. ¿Deseas agregar algo más antes de cerrar?"
                )
            }
        else:
            # pago_en_tienda -> quedamos esperando confirmación corta
            ctx = _ctx_load(pedido)
            ctx["awaiting_confirmation"] = True
            _ctx_save(db, session_id, ctx)
            return {
                "response": (
                    "Listo. Pagas directamente en la tienda al recoger tu pedido. "
                    "¿Te confirmo el pedido ya o quieres agregar otra prenda?"
                )
            }

    if confirm_match or (intent_det["intent"] == "confirmar" and intent_det["confidence"] >= 0.6):
        actualizar_pedido_por_sesion(db, session_id, "estado", "confirmado")
        pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
        if not getattr(pedido_actualizado, "numero_confirmacion", None):
            numero = _gen_numero_confirmacion()
            actualizar_pedido_por_sesion(db, session_id, "numero_confirmacion", numero)
            pedido_actualizado = obtener_pedido_por_sesion(db, session_id)

        try:
            print("🟢 Trigger HubSpot (confirmación por intención/regex). Session:", session_id)
            enviar_pedido_a_hubspot(pedido_actualizado)
        except Exception as e:
            print("❌ HubSpot error (confirmación/regex):", repr(e))

        try:
            mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
            await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
        except Exception as e:
            print("❌ Error alerta interna:", repr(e))

        carrito = carrito_load(pedido_actualizado)
        lineas = cart_summary_lines(carrito)
        resumen = "\n".join(lineas)
        metodo_entrega = (pedido_actualizado.metodo_entrega or "").replace("_", " ")
        metodo_pago = (pedido_actualizado.metodo_pago or "").replace("_", " ")

        return {
            "response": (
                f"¡Pedido confirmado!\n\nNúmero de confirmación: {pedido_actualizado.numero_confirmacion}\n\n"
                f"Resumen:\n{resumen}\n\n"
                f"Entrega: {metodo_entrega or 'pendiente'}\n"
                f"Pago: {metodo_pago or 'pendiente'}\n\n"
                "Te contactaremos en breve para coordinar el siguiente paso. "
                "¿Quieres agregar algo más?"
            )
        }

# -------------------------------------------------------------------
# LLM general
# -------------------------------------------------------------------
async def procesar_conversacion_llm(pedido, texto_usuario: str):
    if client is None:
        carrito = carrito_load(pedido)
        lineas = cart_summary_lines(carrito)
        return {
            "campos": {},
            "respuesta": "Puedo continuar con tu compra. ¿Te muestro camisas o jeans?\n\n" + "\n".join(lineas),
            "acciones": []
        }

    estado = {
        "producto": pedido.producto or None,
        "talla": pedido.talla or None,
        "cantidad": pedido.cantidad or None,
        "metodo_entrega": pedido.metodo_entrega or None,
        "direccion": pedido.direccion or None,
        "punto_venta": pedido.punto_venta or None,
        "metodo_pago": pedido.metodo_pago or None,
        "estado": pedido.estado,
    }

    extras = {}
    productos = []
    mensaje = None

    # Sugerencias de productos
    try:
        sug = sugerir_productos(texto_usuario, limite=3)
        if isinstance(sug, dict):
            productos = (sug.get("productos") or [])[:3]
            mensaje = sug.get("mensaje")
    except Exception:
        productos = []

    if not productos:
        try:
            cat, _ = detectar_categoria(texto_usuario)
        except Exception:
            cat = None
        if cat:
            sug2 = sugerir_productos(cat, limite=3)
            if isinstance(sug2, dict):
                productos = (sug2.get("productos") or [])[:3]
                if not mensaje:
                    mensaje = sug2.get("mensaje")

    if productos:
        extras["productos_disponibles"] = productos
    elif mensaje:
        extras["mensaje_sugerencias"] = mensaje

    if estado["metodo_entrega"] == "recoger_en_tienda" and not estado["punto_venta"]:
        extras["puntos_venta"] = PUNTOS_VENTA

    instruct_json = (
        "Devuelve un JSON con las claves: 'campos', 'respuesta' y opcionalmente 'acciones'. "
        "'campos' incluye solo datos del pedido que quieras actualizar "
        "(producto, talla, cantidad, método de entrega, dirección, punto_venta, método de pago, estado, email, telefono). "
        "'respuesta' es un texto humano, natural y profesional como asesor de CASSANY (sin emojis ni listas numeradas). "
        "'acciones' (opcional) es una lista de objetos. Cada objeto tiene 'tipo' y 'args'. "
        "Tipos permitidos:\n"
        "- 'add_item' -> args: {sku, nombre, categoria, talla?, color?, cantidad, precio_unitario}\n"
        "- 'update_qty' -> args: {sku, talla?, color?, cantidad}\n"
        "- 'remove_item' -> args: {sku, talla?, color?}\n"
        "- 'show_cart' -> args: {}\n"
        "- 'finalize_order' -> args: {metodo_pago?, sucursal?}\n"
        "- 'remember_pref' -> args: {categoria?, talla?, color_favorito?}\n"
        "- 'cache_list' -> args: {productos: [ {nombre, url, precio, tallas_disponibles?} ... ]}\n\n"
        "Si en Contexto_extra hay 'productos_disponibles', preséntalos (máx. 3) con formato '1. Nombre - $precio - URL' "
        "y añade 'cache_list' con los mismos elementos.\n"
        "Si hay 'mensaje_sugerencias', comunícalo brevemente y ofrece algo similar.\n"
        "Antes de pedir datos personales, recuerda la política: https://cassany.co/tratamiento-de-datos-personales/."
    )

    carrito = carrito_load(pedido)
    prefs = _prefs_load(pedido)
    carrito_resumen = "\n".join(cart_summary_lines(carrito))
    perfil = {
        "tallas_preferidas": prefs.get("tallas_preferidas", {}),
        "color_favorito": prefs.get("color_favorito"),
    }

    user_prompt = (
        f"Estado_pedido: {json.dumps(estado, ensure_ascii=False)}"
        f"\nCarrito_resumen: {carrito_resumen}"
        f"\nPerfil_cliente: {json.dumps(perfil, ensure_ascii=False)}"
        f"\nContexto_extra: {json.dumps(extras, ensure_ascii=False)}"
        f"\nUsuario: {texto_usuario}"
    )

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": base_prompt},
                {"role": "system", "content": instruct_json},
                {"role": "system", "content": ACTIONS_PROTOCOL},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=1000,
            response_format={"type": "json_object"}
        )
        raw = completion.choices[0].message.content.strip()
        data = json.loads(raw)

        # Normaliza tallas dentro de cache_list
        if isinstance(data, dict):
            acts = data.get("acciones") or []
            norm_acts = []
            for a in acts or []:
                if isinstance(a, dict) and "tipo" in a and "args" in a:
                    if a.get("tipo") == "cache_list":
                        prods2 = (a.get("args") or {}).get("productos") or []
                        for p in prods2:
                            if isinstance(p, dict) and "tallas_disponibles" in p:
                                p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                norm_acts.append(a)
            if norm_acts:
                data["acciones"] = norm_acts

        return data
    except Exception:
        return {"campos": {}, "respuesta": "¿Quieres ver camisas o jeans?", "acciones": []}

# --- limpiezas de tallas/preferencias ---
TALLAS_VALIDAS = {"XS","S","M","L","XL","XXL","28","30","32","34","36","38","40","42"}
def _clean_tallas(arr):
    if not isinstance(arr, list):
        return []
    return [t for t in dict.fromkeys([str(t).upper() for t in (arr or [])]) if t in TALLAS_VALIDAS]

def _prefs_load(pedido) -> dict:
    try:
        sid = pedido.session_id
        db = SessionLocal()
        try:
            row = db.execute(sa_text("SELECT preferencias_json FROM pedidos WHERE session_id=:sid"),
                             {"sid": sid}).fetchone()
        finally:
            db.close()
        raw = row[0] if row and row[0] else "{}"
        data = _safe_json_load(raw, {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _prefs_save(db: Session, session_id: str, prefs: dict):
    try:
        db.execute(sa_text("UPDATE pedidos SET preferencias_json=:j WHERE session_id=:sid"),
                   {"j": json.dumps(prefs, ensure_ascii=False), "sid": session_id})
        db.commit()
    except Exception:
        db.rollback()

# -------------------------------------------------------------------
# Action Protocol (SHOW_CART / ADD_TO_CART / etc.)
# -------------------------------------------------------------------
def _resolve_product_ref(db: Session, session_id: str, ref: str) -> Optional[dict]:
    lista = _get_sugeridos_list(db, session_id) or []
    if not ref:
        return lista[0] if len(lista) == 1 else None
    ref = str(ref).strip()
    if ref.isdigit():
        i = int(ref) - 1
        if 0 <= i < len(lista):
            return lista[i]
    for p in lista:
        if ref == p.get("url") or ref == p.get("sku") or ref.lower() == (p.get("nombre","").lower()):
            return p
    return None

def _handle_action_protocol(payload: dict, db: Session, session_id: str, pedido) -> Optional[dict]:
    if not isinstance(payload, dict) or "action" not in payload:
        return None

    action = payload.get("action")

    if action == "SHOW_CART":
        carrito = carrito_load(pedido)
        return {"response": "\n".join(cart_summary_lines(carrito))}

    if action == "ASK_VARIANT":
        ref = payload.get("product_ref")
        prod = _resolve_product_ref(db, session_id, ref) if ref else None
        if prod:
            ctx = _ctx_load(pedido)
            ctx["pending_variant"] = {
                "ref": ref,
                "sku": prod.get("sku") or prod.get("url") or prod.get("nombre"),
                "qty": int(payload.get("qty") or 1),
            }
            _ctx_save(db, session_id, ctx)
            tallas = _clean_tallas(prod.get("tallas_disponibles") or [])
            if tallas:
                return {"response": f"Para «{prod.get('nombre','Producto')}», ¿qué talla prefieres? Opciones: {', '.join(tallas)}"}
            return {"response": "¿Qué talla prefieres?"}
        return {"response": "¿De cuál producto necesitas la talla? Indícame el número (1, 2 o 3) o envíame el enlace."}

    if action == "CLARIFY":
        return {"response": payload.get("question") or "¿Podrías confirmar qué producto?"}

    if action == "REMOVE_FROM_CART":
        carrito = carrito_load(pedido)
        sku = str(payload.get("product_id") or payload.get("sku") or "")
        talla = payload.get("size")
        color = payload.get("color")
        carrito = cart_remove(carrito, sku, talla, color)
        carrito_save(db, session_id, carrito)
        try:
            actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
        except Exception:
            pass
        return {"response": "\n".join(cart_summary_lines(carrito))}

    if action == "ADD_TO_CART":
        prod = _resolve_product_ref(db, session_id, payload.get("product_ref"))
        if not prod:
            lista = _get_sugeridos_list(db, session_id) or []
            if len(lista) == 1:
                prod = lista[0]
            if not prod:
                return {"response": "No identifiqué el producto. Dime el número de la opción (1, 2 o 3) o envíame el link."}

        tallas = _clean_tallas(prod.get("tallas_disponibles") or [])
        size = payload.get("size")
        if tallas and not size:
            return {"response": f"Para agregar «{prod.get('nombre','Producto')}» necesito la talla: {', '.join(tallas)}. ¿Cuál prefieres?"}
        if tallas and size and str(size).upper() not in tallas:
            return {"response": f"Para «{prod.get('nombre','Producto')}» tengo {', '.join(tallas)}. ¿Quieres elegir una de esas tallas?"}

        carrito = carrito_load(pedido)
        carrito = cart_add(
            carrito,
            sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
            nombre=prod.get("nombre","Producto"),
            categoria=prod.get("categoria",""),
            talla=str(size).upper() if isinstance(size, str) else size,
            color=payload.get("color") or prod.get("color"),
            cantidad=int(payload.get("qty") or 1),
            precio_unitario=float(prod.get("precio", 0.0)),
        )
        carrito_save(db, session_id, carrito)
        try:
            actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
        except Exception:
            pass
        return {"response": "Agregado al carrito ✅\n\n" + "\n".join(cart_summary_lines(carrito))}

    return None

# -------------------------------------------------------------------
# ENDPOINT principal de conversación
# -------------------------------------------------------------------
@router.post("/mensaje-whatsapp")
async def mensaje_whatsapp(user_input: UserMessage, session_id: str, db: Session = Depends(get_db)):
    ahora = datetime.now(timezone.utc)
    pedido = obtener_pedido_por_sesion(db, session_id)

    if not pedido:
        crear_pedido(db, {
            "session_id": session_id,
            "producto": "",
            "cantidad": 0,
            "talla": "",
            "precio_unitario": 0.0,
            "nombre_cliente": "",
            "direccion": "",
            "ciudad": "",
            "metodo_pago": "",
            "metodo_entrega": "",
            "punto_venta": "",
            "notas": "",
            "estado": "pendiente",
            "last_activity": ahora,
            "datos_personales_advertidos": False,
            "saludo_enviado": 0,
            "last_msg_id": None,
            "sugeridos": "",
            "ctx_json": "{}",
            "ultima_categoria": "",
            "ultimos_filtros": "",
            "sugeridos_json": "[]",
            "carrito_json": "[]",
            "preferencias_json": "{}",
            "numero_confirmacion": "",
        })
        pedido = obtener_pedido_por_sesion(db, session_id)

    last_act_utc = getattr(pedido, "last_activity", ahora.replace(tzinfo=UTC))
    if not isinstance(last_act_utc, datetime):
        last_act_utc = ahora.replace(tzinfo=UTC)
    tiempo_inactivo = ahora - last_act_utc

    raw_text = user_input.message.strip()
    user_text = raw_text.lower()

    # capturar nombre libre
    mname = NOMBRE_RE.search(raw_text or "")
    if mname and not getattr(pedido, "nombre_cliente", None):
        name = " ".join(w.capitalize() for w in mname.group(1).split())
        actualizar_pedido_por_sesion(db, session_id, "nombre_cliente", name)

    actualizar_pedido_por_sesion(db, session_id, "last_activity", ahora)

    # filtros del mensaje
    filtros_detectados = {}
    m_color = COLOR_RE.search(user_text)
    if m_color:
        filtros_detectados["color"] = m_color.group(1).lower()
    m_talla_tok = TALLA_TOKEN_RE.search(user_text)
    if m_talla_tok:
        talla_val = (m_talla_tok.group(1) or "").upper()
        if talla_val in TALLAS_VALIDAS:
            filtros_detectados["talla"] = talla_val
    m_manga = MANGA_RE.search(user_text)
    if m_manga:
        filtros_detectados["manga"] = m_manga.group(1).lower()
    m_uso = USO_RE.search(user_text)
    if m_uso:
        filtros_detectados["uso"] = m_uso.group(1).lower()
    if filtros_detectados:
        set_user_filter(db, session_id, filtros_detectados)

    # Si llegó talla y hay pending_variant => agrega y limpia pending
    if "talla" in filtros_detectados:
        ctx = _ctx_load(pedido)
        pv_ctx = ctx.get("pending_variant")
        if pv_ctx:
            prod = _resolve_product_ref(db, session_id, pv_ctx.get("ref") or pv_ctx.get("sku"))
            if prod:
                tallas = _clean_tallas(prod.get("tallas_disponibles") or [])
                if tallas and filtros_detectados["talla"] not in tallas:
                    return {"response": f"Para «{prod.get('nombre','Producto')}» tengo {', '.join(tallas)}. ¿Quieres elegir una de esas tallas?"}
                carrito = carrito_load(pedido)
                carrito = cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=filtros_detectados["talla"],
                    color=prod.get("color"),
                    cantidad=int(pv_ctx.get("qty") or 1),
                    precio_unitario=float(prod.get("precio", 0.0)),
                )
                carrito_save(db, session_id, carrito)
                try:
                    actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
                except Exception:
                    pass
                ctx.pop("pending_variant", None)
                _ctx_save(db, session_id, ctx)
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(cart_summary_lines(carrito))}

    # awaiting qty por contexto (producto sin tallas)
    ctx_qty = _ctx_load(pedido)
    awaiting = ctx_qty.get("awaiting_qty")
    if awaiting:
        qty = extract_qty(user_text)
        if qty:
            ref = str(awaiting.get("ref") or "")
            prod = _resolve_product_ref(db, session_id, ref) or _resolve_product_ref(db, session_id, awaiting.get("sku") or "")
            if prod:
                carrito = carrito_load(pedido)
                carrito = cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=None,
                    color=prod.get("color"),
                    cantidad=int(qty),
                    precio_unitario=float(prod.get("precio", 0.0) or 0.0),
                )
                carrito_save(db, session_id, carrito)
                try:
                    actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
                except Exception:
                    pass
                ctx_qty.pop("awaiting_qty", None)
                _ctx_save(db, session_id, ctx_qty)
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(cart_summary_lines(carrito))}
        return {"response": "¿Cuántas unidades deseas? (por ejemplo: 1, 2 o 3)"}

    # fast-path talla sola (última selección)
    m_talla_solo = TALLA_TOKEN_RE.search(user_text)
    if m_talla_solo and not MOSTRAR_RE.search(user_text):
        talla_elegida = (m_talla_solo.group(1) or "").upper()
        if talla_elegida in TALLAS_VALIDAS:
            ctx_last = _ctx_load(pedido)
            last = (ctx_last.get("selecciones") or [])[-1] if ctx_last.get("selecciones") else None
            if last:
                lista = _get_sugeridos_list(db, session_id)
                prod = next((p for p in (lista or []) if p.get("url") == last.get("url") or p.get("nombre") == last.get("nombre")), None) or last
                tallas = _clean_tallas(prod.get("tallas_disponibles"))
                if tallas and talla_elegida not in tallas:
                    return {"response": f"Para «{prod.get('nombre','Producto')}» tengo {', '.join(tallas)}. ¿Quieres elegir una de esas tallas?"}
                carrito = carrito_load(pedido)
                carrito = cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=talla_elegida,
                    color=prod.get("color"),
                    cantidad=1,
                    precio_unitario=float(prod.get("precio", 0.0))
                )
                carrito_save(db, session_id, carrito)
                try:
                    actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
                except Exception:
                    pass
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(cart_summary_lines(carrito))}

    # confirmación corta por contexto
    ctx_tmp = _ctx_load(pedido)
    if ctx_tmp.get("awaiting_confirmation"):
        if re.search(r'^(s[ií]|ok|dale|listo|de acuerdo|est(a|á)\s*bien|as(i|í)\s*est(a|á)\s*bien)\b', user_text, re.I):
            actualizar_pedido_por_sesion(db, session_id, "estado", "confirmado")
            pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
            if not getattr(pedido_actualizado, "numero_confirmacion", None):
                numero = _gen_numero_confirmacion()
                actualizar_pedido_por_sesion(db, session_id, "numero_confirmacion", numero)
                                pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
            try:
                ctx_tmp.pop("awaiting_confirmation", None)
                _ctx_save(db, session_id, ctx_tmp)
            except Exception:
                pass
            try:
                enviar_pedido_a_hubspot(pedido_actualizado)
            except Exception as e:
                print("❌ HubSpot error (confirmación corta):", repr(e))
            try:
                mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
                await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
            except Exception as e:
                print("❌ Error alerta interna (confirmación corta):", repr(e))

            carrito_ok = carrito_load(pedido_actualizado)
            resumen = "\n".join(cart_summary_lines(carrito_ok))
            metodo_entrega = (pedido_actualizado.metodo_entrega or "").replace("_", " ")
            metodo_pago = (pedido_actualizado.metodo_pago or "").replace("_", " ")
            return {
                "response": (
                    f"¡Pedido confirmado!\n\nNúmero de confirmación: {pedido_actualizado.numero_confirmacion}\n\n"
                    f"Resumen:\n{resumen}\n\n"
                    f"Entrega: {metodo_entrega or 'pendiente'}\n"
                    f"Pago: {metodo_pago or 'pendiente'}\n\n"
                    "Te contactaremos en breve para coordinar el siguiente paso. "
                    "¿Quieres agregar algo más?"
                )
            }

    # Manejo de pago/confirmación (router híbrido)
    resp_pago = await procesar_mensaje_usuario(user_text, db, session_id, pedido)
    if resp_pago:
        return resp_pago

    # Autoregistro de teléfono desde session_id si aplica (cliente_57...)
    if not getattr(pedido, "telefono", None) and session_id.startswith("cliente_"):
        telefono_cliente = session_id.replace("cliente_", "")
        actualizar_pedido_por_sesion(db, session_id, "telefono", telefono_cliente)

    # Derivar a atención humana
    if detectar_intencion_atencion(user_text):
        try:
            mensaje_alerta = generar_mensaje_atencion_humana(pedido)
            await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
        except Exception as e:
            print("❌ Error alerta humana:", repr(e))
        return {"response": "Entendido, ya te pongo en contacto con uno de nuestros asesores. Te responderán personalmente en breve."}

    # Cancelación explícita
    if any(neg in user_text for neg in ["ya no quiero", "cancelar pedido", "no deseo", "me arrepentí", "me arrepenti"]):
        producto_cancelado = pedido.producto or "el pedido actual"
        actualizar_pedido_por_sesion(db, session_id, "estado", "cancelado")
        actualizar_pedido_por_sesion(db, session_id, "producto", "")
        actualizar_pedido_por_sesion(db, session_id, "talla", "")
        actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "")
        actualizar_pedido_por_sesion(db, session_id, "punto_venta", "")
        return {"response": f"Entiendo, he cancelado {producto_cancelado}. ¿Te gustaría ver otra prenda o necesitas ayuda con algo más?"}

    # Saludo
    if SALUDO_RE.match(user_text):
        if _get_saludo_enviado(db, session_id) == 0:
            actualizar_pedido_por_sesion(db, session_id, "saludo_enviado", 1)
            tiendas_txt = (
                "C.C Fabricato – 3103380995\n"
                "C.C Florida – 3207335493\n"
                "Centro - Junín – 3207339281\n"
                "C.C La Central – 3207338021\n"
                "C.C Mayorca – 3207332984\n"
                "C.C Premium Plaza – 3207330457\n"
                "C.C Unicentro – 3103408952"
            )
            saludo = (
                "Bienvenido a CASSANY. Estoy aquí para ayudarte con tu compra.\n"
                "Si prefieres, también puedes comunicarte directamente con la tienda de tu preferencia por WhatsApp.\n\n"
                + tiendas_txt
            )
            return {"response": saludo}

        carrito = carrito_load(pedido)
        if carrito or getattr(pedido, "metodo_entrega", "") or getattr(pedido, "producto", ""):
            lineas = cart_summary_lines(carrito)
            faltan = _pedido_missing_fields(pedido)
            pregunta = _prompt_for_missing(pedido, faltan) if faltan else "¿Confirmo tu pedido?"
            return {"response": "\n".join(lineas) + ("\n\n" + pregunta if pregunta else "")}
        return {"response": "¡Hola! ¿Qué te gustaría ver hoy: camisas, jeans, pantalones o suéteres?"}

    # Entrega
    if DOMICILIO_RE.search(user_text):
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "domicilio")
        if not getattr(pedido, "datos_personales_advertidos", False):
            actualizar_pedido_por_sesion(db, session_id, "datos_personales_advertidos", True)
            return {
                "response": (
                    "Antes de continuar, ten en cuenta que tus datos personales serán tratados "
                    "bajo nuestra política: https://cassany.co/tratamiento-de-datos-personales/\n\n"
                    "Ahora, ¿podrías proporcionarme tu dirección y ciudad para el envío?"
                )
            }
        return {"response": "Perfecto, por favor indícame tu dirección y ciudad para el envío."}

    if RECOGER_RE.search(user_text):
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "recoger_en_tienda")
        tiendas = "\n".join(PUNTOS_VENTA)
        return {"response": f"Por favor, confirma en cuál de nuestras tiendas deseas recoger tu pedido:\n{tiendas}"}

    # Smalltalk (no repregunta si no hace falta)
    if SMALLTALK_RE.search(user_text):
        carrito = carrito_load(pedido)
        if carrito or getattr(pedido, "metodo_entrega", "") or getattr(pedido, "producto", ""):
            lineas = cart_summary_lines(carrito)
            faltan = _pedido_missing_fields(pedido)
            pregunta = _prompt_for_missing(pedido, faltan) if faltan else ""
            base = "\n".join(lineas)
            return {"response": (base + ("\n\n" + pregunta if pregunta else "")).strip()}
        cats = ", ".join(CATEGORIAS_RESUMEN[:4]) + "…"
        return {"response": f"¡Con gusto! ¿Te muestro algo hoy? Tenemos {cats} ¿Qué prefieres ver primero?"}

    # Offtopic → redirige a catálogo
    if OFFTOPIC_RE.search(user_text):
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {"response": "Somos CASSANY, una marca de ropa para hombre. Trabajamos estas categorías:\n" + f"{cats}\n\n" + "¿Te muestro camisas o prefieres otra categoría?"}

    # Discovery
    if DISCOVERY_RE.search(user_text):
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {
            "response": (
                "¡Te ayudo a elegir! Dime por favor:\n"
                "1) ¿Qué te interesa ver primero?\n"
                f"{cats}\n"
                "2) ¿Cuál es tu talla? (S, M, L, XL)\n"
                "3) ¿Tienes ocasión o estilo en mente? (oficina, casual, evento)\n"
                "Con eso te muestro opciones acertadas."
            )
        }

    # Ver carrito
    if CARRO_RE.search(user_text):
        carrito = carrito_load(pedido)
        return {"response": "\n".join(cart_summary_lines(carrito))}

    # Petición de fotos de algo (ruta de “mostrar” por categoría)
    m_fotos = FOTOS_RE.search(user_text)
    if m_fotos:
        cat_txt = m_fotos.group(2).strip()
        try:
            cat, _ = detectar_categoria(cat_txt)
        except Exception:
            cat = None
        consulta = cat or cat_txt

        urls_previas = _get_sugeridos_urls(db, session_id)
        res = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)
        productos = res.get("productos", []) if isinstance(res, dict) else []
        if productos:
            for p in productos:
                if isinstance(p, dict) and "tallas_disponibles" in p:
                    p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
            _remember_list(db, session_id, cat or "", detectar_atributos(cat_txt) or {}, productos)
            _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if p.get("url")])
            lines = []
            for i, pr in enumerate(productos[:3], 1):
                lines.append(f"{i}. {pr.get('nombre','Producto')} - {fmt_cop(pr.get('precio',0))} - {pr.get('url','')}")
            return {"response": "Aquí tienes algunas opciones:\n" + "\n".join(lines)}
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {"response": f"No hay stock para «{cat_txt}» en este momento. ¿Te muestro algo de:\n{cats}"}

    # “Muéstrame …”
    if MOSTRAR_RE.search(user_text):
        try:
            cat, _ = detectar_categoria(user_text)
        except Exception:
            cat = None
        ultima_cat, _ult = _get_ultima_cat_filters(db, session_id)
        consulta = cat or ultima_cat
        if consulta:
            urls_previas = _get_sugeridos_urls(db, session_id)
            res = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)
            productos = res.get("productos", []) if isinstance(res, dict) else []
            if productos:
                for p in productos:
                    if isinstance(p, dict) and "tallas_disponibles" in p:
                        p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                _set_sugeridos_list(db, session_id, productos)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if p.get("url")])
                lines = []
                for i, pr in enumerate(productos[:3], 1):
                    lines.append(f"{i}. {pr.get('nombre','Producto')} - {fmt_cop(pr.get('precio',0))} - {pr.get('url','')}")
                return {"response": "Aquí tienes algunas opciones:\n" + "\n".join(lines)}
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {"response": f"¿Qué te muestro primero?\n{cats}"}

    # Más opciones (siguiente página)
    if MAS_OPCIONES_RE.search(user_text):
        productos_previos = _get_sugeridos_list(db, session_id)
        if productos_previos:
            restantes = productos_previos[3:] if len(productos_previos) > 3 else []
            if restantes:
                for p in restantes:
                    if isinstance(p, dict) and "tallas_disponibles" in p:
                        p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                _set_sugeridos_list(db, session_id, restantes)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in restantes if p.get("url")])
                lines = []
                for i, pr in enumerate(restantes[:3], 1):
                    lines.append(f"{i}. {pr.get('nombre','Producto')} - {fmt_cop(pr.get('precio',0))} - {pr.get('url','')}")
                return {"response": "Aquí tienes más opciones:\n" + "\n".join(lines)}
            return {"response": "Ya te mostré todas las opciones disponibles por ahora. ¿Quieres buscar algo diferente?"}
        return {"response": "Primero dime qué categoría te interesa (p. ej., camisas, jeans, pantalones) y te muestro opciones."}

    # Selección por ordinal
    m_ord = ORDINAL_RE.search(user_text)
    if m_ord:
        idx0 = ORDINALES_MAP[m_ord.group(1).lower()] - 1
        lista = _get_sugeridos_list(db, session_id)
        if lista and 0 <= idx0 < len(lista):
            prod = lista[idx0]
            actualizar_pedido_por_sesion(db, session_id, "producto", prod.get("nombre", ""))
            actualizar_pedido_por_sesion(db, session_id, "precio_unitario", prod.get("precio", 0.0))
            if not getattr(pedido, "cantidad", 0):
                actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
            try:
                _remember_selection(db, session_id, prod, idx0 + 1)
            except Exception:
                pass
            tallas = _clean_tallas(prod.get("tallas_disponibles") or [])
            if ADD_RE.search(user_text):
                if tallas:
                    return {"response": f"Perfecto. Para agregar «{prod.get('nombre','Producto')}» dime la talla ({', '.join(tallas)})."}
                carrito = carrito_load(pedido)
                carrito = cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=None,
                    color=prod.get("color"),
                    cantidad=1,
                    precio_unitario=float(prod.get("precio", 0.0))
                )
                carrito_save(db, session_id, carrito)
                actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(cart_summary_lines(carrito))}
            if tallas:
                return {"response": f"Listo, seleccionaste la opción {idx0+1}. Tallas disponibles: {', '.join(tallas)}. ¿Cuál prefieres?"}
            ctx_set = _ctx_load(pedido)
            ctx_set["awaiting_qty"] = {"ref": idx0 + 1, "sku": prod.get("sku") or prod.get("url") or prod.get("nombre")}
            _ctx_save(db, session_id, ctx_set)
            return {"response": f"Listo, seleccionaste la opción {idx0+1}. ¿Cuántas unidades deseas?"}
        elif lista:
            return {"response": f"Por favor indícame un número entre 1 y {len(lista)} de la lista que te mostré."}

    # Selección por número (“opción 2”, “la 3”, “2”)
    m_sel = SELECCION_RE.search(user_text)
    if m_sel:
        num_txt = next((g for g in m_sel.groups() if g), None)
        if num_txt:
            idx = int(num_txt) - 1
            lista = _get_sugeridos_list(db, session_id)
            if lista and 0 <= idx < len(lista):
                prod = lista[idx]
                actualizar_pedido_por_sesion(db, session_id, "producto", prod.get("nombre", ""))
                actualizar_pedido_por_sesion(db, session_id, "precio_unitario", prod.get("precio", 0.0))
                if not getattr(pedido, "cantidad", 0):
                    actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
                try:
                    _remember_selection(db, session_id, prod, idx + 1)
                except Exception:
                    pass

                tallas = _clean_tallas(prod.get("tallas_disponibles") or [])
                if ADD_RE.search(user_text):
                    if tallas:
                        return {"response": f"Perfecto. Para agregar «{prod.get('nombre','Producto')}» necesito la talla: {', '.join(tallas)}. ¿Cuál prefieres?"}
                    carrito = carrito_load(pedido)
                    carrito = cart_add(
                        carrito,
                        sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                        nombre=prod.get("nombre","Producto"),
                        categoria=prod.get("categoria",""),
                        talla=None,
                        color=prod.get("color"),
                        cantidad=1,
                        precio_unitario=float(prod.get("precio", 0.0))
                    )
                    carrito_save(db, session_id, carrito)
                    try:
                        actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
                    except Exception:
                        pass
                    return {"response": "Agregado al carrito ✅\n\n" + "\n".join(cart_summary_lines(carrito))}
                if tallas:
                    return {"response": f"Listo, seleccionaste la opción {idx+1}. Tallas disponibles: {', '.join(tallas)}. ¿Cuál prefieres?"}
                ctx_set = _ctx_load(pedido)
                ctx_set["awaiting_qty"] = {"ref": idx + 1, "sku": prod.get("sku") or prod.get("url") or prod.get("nombre")}
                _ctx_save(db, session_id, ctx_set)
                return {"response": f"Listo, seleccionaste la opción {idx+1}. ¿Cuántas unidades deseas?"}
            if lista:
                return {"response": f"Por favor indícame un número entre 1 y {len(lista)} de la lista que te mostré."}

    # ======= LLM (flujo general) =======
    resultado = await procesar_conversacion_llm(pedido, user_text)

    handled = _handle_action_protocol(resultado, db, session_id, pedido)
    if handled:
        return handled

    if not isinstance(resultado, dict):
        return {"response": "Disculpa, ocurrió un error procesando tu solicitud. ¿Te muestro opciones de camisas o jeans?"}

    # Normaliza y aplica acciones
    if "acciones" in resultado and isinstance(resultado["acciones"], list):
        carrito = carrito_load(pedido)
        prefs = _prefs_load(pedido)
        for act in resultado["acciones"]:
            try:
                t = (act.get("tipo") or "").strip()
                args = act.get("args") or {}
                if t == "add_item":
                    carrito = cart_add(
                        carrito,
                        sku=args["sku"],
                        nombre=args.get("nombre","Producto"),
                        categoria=args.get("categoria",""),
                        talla=args.get("talla"),
                        color=args.get("color"),
                        cantidad=int(args.get("cantidad", 1)),
                        precio_unitario=float(args.get("precio_unitario", 0.0))
                    )
                elif t == "update_qty":
                    carrito = cart_update_qty(
                        carrito,
                        sku=args["sku"],
                        talla=args.get("talla"),
                        color=args.get("color"),
                        cantidad=int(args.get("cantidad", 1))
                    )
                elif t == "remove_item":
                    carrito = cart_remove(
                        carrito,
                        sku=args["sku"],
                        talla=args.get("talla"),
                        color=args.get("color")
                    )
                elif t == "remember_pref":
                    cat = args.get("categoria")
                    talla = args.get("talla")
                    color_fav = args.get("color_favorito")
                    prefs.setdefault("tallas_preferidas", {})
                    if cat and talla:
                        prefs["tallas_preferidas"][cat] = talla
                    if color_fav:
                        prefs["color_favorito"] = color_fav
                elif t == "cache_list":
                    productos = args.get("productos") or []
                    if isinstance(productos, list) and productos:
                        for p in productos:
                            if isinstance(p, dict) and "tallas_disponibles" in p:
                                p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                        try:
                            _set_sugeridos_list(db, session_id, productos)
                            _append_sugeridos_urls(db, session_id, [p.get("url") for p in productos if isinstance(p, dict) and p.get("url")])
                        except Exception:
                            pass
            except Exception:
                continue

        carrito_save(db, session_id, carrito)
        _prefs_save(db, session_id, prefs)
        try:
            actualizar_pedido_por_sesion(db, session_id, "subtotal", cart_total(carrito))
        except Exception:
            pass

    # Guardar campos del LLM si llegaron
    campos_dict = resultado.get("campos", {}) or {}
    if "nombre_completo" in campos_dict and "nombre_cliente" not in campos_dict:
        campos_dict["nombre_cliente"] = campos_dict.pop("nombre_completo")

    if isinstance(campos_dict, dict):
        if "producto" in campos_dict:
            actualizar_pedido_por_sesion(db, session_id, "talla", "")
            actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
        for campo, val in campos_dict.items():
            if campo in {
                "producto","talla","cantidad","metodo_entrega","direccion",
                "punto_venta","metodo_pago","estado","nombre_cliente","telefono",
                "email","ciudad","precio_unitario","subtotal"
            }:
                actualizar_pedido_por_sesion(db, session_id, campo, val)
        if ("talla" in campos_dict) or ("cantidad" in campos_dict):
            _update_last_selection_from_pedido(db, session_id)

    # Preguntas específicas
    if campos_dict.get("metodo_entrega") == "recoger_en_tienda" and not campos_dict.get("punto_venta"):
        tiendas = "\n".join(PUNTOS_VENTA)
        return {"response": f"Por favor, confirma en cuál de nuestras tiendas deseas recoger tu pedido:\n{tiendas}"}

    if campos_dict.get("direccion") and campos_dict.get("ciudad"):
        return {
            "response": (
                "Perfecto, he registrado tu dirección y ciudad.\n\n"
                "Por favor, confirma el método de pago que prefieres:\n"
                f"- Transferencia a Bancolombia: Cuenta Corriente No. {CTA_BANCOLOMBIA}\n"
                f"- Transferencia a Davivienda: Cuenta Corriente No. {CTA_DAVIVIENDA}\n"
                "- Pago con PayU desde nuestro sitio web."
            )
        }

    if campos_dict.get("metodo_pago") == "transferencia":
        return {
            "response": (
                "Perfecto, para finalizar por favor envía el comprobante de la transferencia por este chat. "
                "Un asesor revisará tu pago y confirmará tu pedido en breve."
            )
        }

    if campos_dict.get("estado") == "confirmado":
        pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
        if not getattr(pedido_actualizado, "numero_confirmacion", None):
            numero = _gen_numero_confirmacion()
            actualizar_pedido_por_sesion(db, session_id, "numero_confirmacion", numero)
            pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
        try:
            enviar_pedido_a_hubspot(pedido_actualizado)
        except Exception as e:
            print("❌ HubSpot error (estado confirmado):", repr(e))
        try:
            mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
            await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
        except Exception as e:
            print("❌ Error alerta interna (estado confirmado):", repr(e))

    # Verificador de faltantes (una cosa a la vez, sin repreguntar si ya hay carrito)
    pedido_refresco = obtener_pedido_por_sesion(db, session_id)
    faltan = _pedido_missing_fields(pedido_refresco)
    if faltan:
        pregunta = _prompt_for_missing(pedido_refresco, faltan)
        base = (resultado.get("respuesta") or "").strip()
        if pregunta:
            return {"response": (base + ("\n\n" if base else "") + pregunta).strip()}

    return {"response": resultado.get("respuesta", "Disculpa, ocurrió un error.")}

# -------------------------------------------------------------------
# Utilidades varias
# -------------------------------------------------------------------
def _gen_numero_confirmacion(prefix="CAS"):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    suf = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{ts}-{suf}"

