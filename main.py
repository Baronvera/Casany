# main.py
APP_BUILD = "build_10"

import os
import json
import re
import unicodedata
import requests  # se mantiene por compatibilidad, aunque ya usamos httpx para WhatsApp
import httpx
import hmac
import hashlib
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, List

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, EmailStr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from models import Pedido
from crud import (
    actualizar_pedido_por_sesion,
    crear_pedido,
    obtener_pedido_por_sesion,
)
from database import SessionLocal, init_db
from hubspot_utils import enviar_pedido_a_hubspot
from utils_intencion import detectar_intencion_atencion
from utils_mensaje_whatsapp import generar_mensaje_atencion_humana
from woocommerce_gpt_utils import sugerir_productos, detectar_categoria, detectar_atributos
from routes_agent import router as agent_router

#  Configuración y cliente OpenAI
load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT_ID")  # opcional
WA_GRAPH_API_VER = os.getenv("WA_GRAPH_API_VER", "v17.0")

# --- ENV centralizados (cuentas, teléfonos, textos) ---
ALERTA_WHATSAPP = os.getenv("ALERTA_WHATSAPP", "+573113305646")
CTA_BANCOLOMBIA = os.getenv("CTA_BANCOLOMBIA", "27480228756")
CTA_DAVIVIENDA = os.getenv("CTA_DAVIVIENDA", "037169997501")
TIENDAS_WHATS = os.getenv(
    "TIENDAS_WHATS",
    "C.C Fabricato – 3103380995\n"
    "C.C Florida – 3207335493\n"
    "Centro - Junín – 3207339281\n"
    "C.C La Central – 3207338021\n"
    "C.C Mayorca – 3207332984\n"
    "C.C Premium Plaza – 3207330457\n"
    "C.C Unicentro – 3103408952"
)
SALUDO_BASE = os.getenv(
    "SALUDO_BASE",
    "Bienvenido a CASSANY. Estoy aquí para ayudarte con tu compra.\n"
    "Si prefieres, también puedes comunicarte directamente con la tienda de tu preferencia por WhatsApp.\n\n"
    + TIENDAS_WHATS
)
WA_APP_SECRET = os.getenv("WA_APP_SECRET", "")

client = None
if OPENAI_API_KEY:
    openai_client_kwargs = {
        "api_key": OPENAI_API_KEY,
        "timeout": 30,
        "max_retries": 2,
    }
    if OPENAI_PROJECT_ID:
        openai_client_kwargs["project"] = OPENAI_PROJECT_ID
    client = OpenAI(**openai_client_kwargs)
else:
    print("⚠️  OPENAI_API_KEY no definido. Arranca sin LLM; endpoints seguirán respondiendo.")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")

try:
    TIMEOUT_MIN = max(1, int(os.getenv("SESSION_TIMEOUT_MIN", "60")))
except ValueError:
    TIMEOUT_MIN = 60

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python 3.8

# --- Zona horaria local (Bogotá) y helpers de tiempo/JSON ---
LOCAL_TZ = ZoneInfo("America/Bogota")
UTC = timezone.utc

def now_utc() -> datetime:
    return datetime.now(UTC)

def parse_db_ts(val) -> datetime:
    if not val:
        return now_utc()
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=UTC)
    if isinstance(val, str):
        s = val.strip()
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            pass
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except Exception:
            pass
    return now_utc()

def to_db_ts(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        dt = now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

def _to_utc(dt: datetime) -> datetime:
    return parse_db_ts(dt)

def _safe_json_load(s: str, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

#  Reglas / constantes
SUPPORT_START_HOUR, SUPPORT_END_HOUR = 9, 19

AREA_METRO_MEDELLIN = {
    "medellín", "medellin", "envigado", "sabaneta", "bello", "itagüí", "itagui",
}

PUNTOS_VENTA = [
    "C.C Premium Plaza",
    "C.C Mayorca",
    "C.C Unicentro",
    "Centro - Colombia",
    "C.C La Central",
    "Centro - Junín",
    "C.C Florida",
]

SALUDO_RE = re.compile(r'^\s*(hola|buenas(?:\s+(tardes|noches))?|buen(?:o|a)s?\s*d[ií]as?|hey)\b', re.I)
MAS_OPCIONES_RE = re.compile(r'\b(más opciones|mas opciones|muéstrame más|muestrame mas|ver más|ver mas)\b', re.I)
DOMICILIO_RE = re.compile(r'\b(a\s*domicilio|env[ií]o\s*a\s*domicilio|domicilio)\b', re.I)
RECOGER_RE  = re.compile(r'\b(recoger(?:lo)?\s+en\s+(tienda|sucursal)|retiro\s+en\s+tienda)\b', re.I)
SELECCION_RE = re.compile(r'(?:opci(?:o|ó)n\s*(\d+))|(?:\bla\s*(\d+)\b)|(?:n[uú]mero\s*(\d+))|^(?:\s*)(\d+)(?:\s*)$', re.I)

# Verbos de “agregar”
ADD_RE = re.compile(r'\b(agrega|agregar|añade|añadir|mete|pon(?:er)?|suma|agregalo|agregá|agregame)\b', re.I)

OFFTOPIC_RE = re.compile(
    r"(qué\s+vend[eé]n?|que\s+vend[eé]n?|qué\s+es\s+cassany|qu[eé]\s+es\s+cassany|"
    r"d[oó]nde\s+est[aá]n|ubicaci[oó]n|horarios?|qu[ií]en(es)?\s+son|historia|"
    r"c[oó]mo\s+funciona|pol[ií]tica(s)?\s+(de\s+)?(cambio|devoluci[oó]n|datos)|"
    r"p[óo]liza|env[ií]os?\s*(nacionales|a\s+d[oó]nde)?|m[ée]todos?\s+de\s+pago)", re.I)
SMALLTALK_RE = re.compile(
    r"^(gracias|muchas gracias|ok|dale|listo|perfecto|bien|super|s[uú]per|genial|jaja+|jeje+|"
    r"vale|de acuerdo|entendido|thanks|okey)\W*$", re.I)
DISCOVERY_RE = re.compile(
    r"(no\s*s[eé]\s*qu[eé]\s*comprar|qu[eé]\s+me\s+(recomiendas|sugieres)|recomi[eé]ndame|"
    r"me\s+ayudas?\s+a\s+elegir|m(u|ú)estrame\s+opciones|quiero\s+ver\s+opciones|"
    r"sugerencias|recomendaci[oó]n)", re.I)
CARRO_RE   = re.compile(r'\b(carrito|mi carrito|ver carrito|ver el carrito|carro|mi pedido|resumen del pedido)\b', re.I)
MOSTRAR_RE = re.compile(r'\b(mu[eé]strame|muestrame|mostrarme|puedes mostrarme|puede mostrarme|podr[ií]as? mostrarme|quiero ver|ens[eñ]a(?:me)?)\b', re.I)
FOTOS_RE   = re.compile(r'\b(fotos?|im[aá]genes?)\s+de\s+([a-záéíóúñü\s]+)\b', re.I)

# Tallas
TALLA_RE = re.compile(r'\btalla\b|\b(XXL|XL|XS|S|M|L)\b', re.I)  # para detectar la palabra
TALLA_TOKEN_RE = re.compile(r'\b(XXL|XL|XS|S|M|L|28|30|32|34|36|38|40|42)\b', re.I)  # para extraer valor real
USO_RE = re.compile(r'\b(oficina|formal|casual|evento|trabajo)\b', re.I)
MANGA_RE = re.compile(r'\bmanga\s+(corta|larga)\b', re.I)
COLOR_RE = re.compile(
    r'\b(blanco|blanca|negro|negra|azul|azules|beige|gris|rojo|verde|café|marr[oó]n|vinotinto|mostaza|'
    r'crema|turquesa|celeste|lila|morado|rosa|rosado|amarillo|naranja)\b', re.I
)

# Ordinales (la primera/segunda/...)
ORDINALES_MAP = {
    "primer": 1, "primera": 1, "primero": 1, "uno": 1, "una": 1,
    "segundo": 2, "segunda": 2, "dos": 2,
    "tercero": 3, "tercera": 3, "tres": 3,
    "cuarto": 4, "cuarta": 4, "cuatro": 4,
    "quinto": 5, "quinta": 5, "cinco": 5,
    "sexto": 6, "sexta": 6, "seis": 6,
    "séptimo": 7, "septimo": 7, "séptima": 7, "septima": 7, "siete": 7,
}
ORDINAL_RE = re.compile(r'\b(' + '|'.join(ORDINALES_MAP.keys()) + r')\b', re.I)

PAGO_RE = re.compile(
    r'(pagar|pago|quiero pagar|voy a pagar|prefiero pagar|el pago|pagaremos|pagare).*(transferencia|bancolombia|davivienda|pse|payu|pago en tienda|efectivo|contraentrega)'
    r'|(transferencia|bancolombia|davivienda|pse|payu|pago en tienda|efectivo|contraentrega).*(pagar|pago|quiero|voy|prefiero|pagaremos|pagare)',
    re.I
)
CONFIRM_RE = re.compile(
    r"\b(confirmar|confirmo|finalizar|cerrar|terminar|realizar)\b.*\b(pedido|compra|orden)\b",
    re.I
)

# --- Cantidades ---
QTY_WORDS_MAP = {
    "un": 1, "uno": 1, "una": 1,
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
}
QTY_WORD_RE = re.compile(r'\b(un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\b', re.I)
QTY_NUM_RE  = re.compile(r'\b(\d{1,2})\b')

def _norm_txt(s: str) -> str:
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

def _extract_qty(texto: str) -> Optional[int]:
    t = _norm_txt(texto)
    m = QTY_WORD_RE.search(t)
    if m:
        return min(10, max(1, int(QTY_WORDS_MAP.get(m.group(1), 1))))
    m = QTY_NUM_RE.search(t)
    if m:
        try:
            return max(1, min(10, int(m.group(1))))
        except Exception:
            return None
    return None

# ----- FastAPI app -----
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # puedes restringir al dominio del webchat
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(agent_router)

def _fmt_cop(v: float) -> str:
    """Formatea a COP con separador de miles y 0 decimales."""
    try:
        return f"${float(v):,.0f}".replace(",", ".")
    except Exception:
        return "$0"

async def procesar_mensaje_usuario(text: str, db, session_id, pedido):
    # 👉 Pago / Confirmación (router híbrido: regex -> LLM clasificador)
    pago_match = PAGO_RE.search(text)
    confirm_match = CONFIRM_RE.search(text)

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
        # Genera número de confirmación si falta
        pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
        if not getattr(pedido_actualizado, "numero_confirmacion", None):
            numero = _gen_numero_confirmacion()
            actualizar_pedido_por_sesion(db, session_id, "numero_confirmacion", numero)
            pedido_actualizado = obtener_pedido_por_sesion(db, session_id)

        try:
            enviar_pedido_a_hubspot(pedido_actualizado)
        except Exception:
            pass
        try:
            mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
            await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
        except Exception:
            pass

        carrito = _carrito_load(pedido_actualizado)
        lineas = _cart_summary_lines(carrito)
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

def _tiene_atributos_especificos(txt: str) -> bool:
    try:
        attrs = detectar_atributos(txt) or {}
    except Exception:
        attrs = {}
    return bool(attrs) or any([
        TALLA_RE.search(txt),
        USO_RE.search(txt),
        MANGA_RE.search(txt),
        COLOR_RE.search(txt),
    ])

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

# Catálogo breve
CATEGORIAS_RESUMEN = [
    "camisas (incluye guayaberas)", "jeans", "pantalones",
    "bermudas", "blazers", "suéteres", "camisetas",
    "calzado", "accesorios"
]
PATRONES_RECHAZO = [
    "esas no", "no me sirven", "no me gusta", "no me gustan", "no la quiero", "esa no es", "no es esa", "ninguna aplica",
    "otra", "otra opción", "otras", "otras opciones",
    "son manga corta", "quiero manga larga"
]

# ---- Limpieza de tallas ----
TALLAS_VALIDAS = {
    "XS", "S", "M", "L", "XL", "XXL",
    "28", "30", "32", "34", "36", "38", "40", "42"
}
def _clean_tallas(arr):
    if not isinstance(arr, list):
        return []
    return [t for t in dict.fromkeys([str(t).upper() for t in (arr or [])]) if t in TALLAS_VALIDAS]

def _has_column(db: Session, table: str, col: str) -> bool:
    try:
        rows = db.execute(sa_text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == col for r in rows)
    except Exception:
        return False

def _ensure_column(col: str, ddl: str, table: str = "pedidos"):
    db = SessionLocal()
    try:
        if not _has_column(db, table, col):
            db.execute(sa_text(ddl))
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

_ensure_column("last_activity",
    "ALTER TABLE pedidos ADD COLUMN last_activity DATETIME DEFAULT (CURRENT_TIMESTAMP)")
_ensure_column("sugeridos",
    "ALTER TABLE pedidos ADD COLUMN sugeridos TEXT")
_ensure_column("punto_venta",
    "ALTER TABLE pedidos ADD COLUMN punto_venta TEXT")
_ensure_column("datos_personales_advertidos",
    "ALTER TABLE pedidos ADD COLUMN datos_personales_advertidos INTEGER DEFAULT 0")
_ensure_column("telefono",
    "ALTER TABLE pedidos ADD COLUMN telefono TEXT")
_ensure_column("saludo_enviado",
    "ALTER TABLE pedidos ADD COLUMN saludo_enviado INTEGER DEFAULT 0")
_ensure_column("last_msg_id",
    "ALTER TABLE pedidos ADD COLUMN last_msg_id TEXT")
_ensure_column("ultima_categoria",
    "ALTER TABLE pedidos ADD COLUMN ultima_categoria TEXT")
_ensure_column("ultimos_filtros",
    "ALTER TABLE pedidos ADD COLUMN ultimos_filtros TEXT")
_ensure_column("sugeridos_json",
    "ALTER TABLE pedidos ADD COLUMN sugeridos_json TEXT")
_ensure_column("ctx_json",
    "ALTER TABLE pedidos ADD COLUMN ctx_json TEXT")
_ensure_column("carrito_json",
    "ALTER TABLE pedidos ADD COLUMN carrito_json TEXT")
_ensure_column("preferencias_json",
    "ALTER TABLE pedidos ADD COLUMN preferencias_json TEXT")
_ensure_column("filtros", "ALTER TABLE pedidos ADD COLUMN filtros TEXT")
_ensure_column("numero_confirmacion", "ALTER TABLE pedidos ADD COLUMN numero_confirmacion TEXT")

def _normalize_nulls():
    db = SessionLocal()
    try:
        db.execute(sa_text("UPDATE pedidos SET saludo_enviado=0 WHERE saludo_enviado IS NULL"))
        db.execute(sa_text("UPDATE pedidos SET datos_personales_advertidos=0 WHERE datos_personales_advertidos IS NULL"))
        db.execute(sa_text("UPDATE pedidos SET sugeridos='' WHERE sugeridos IS NULL"))
        db.execute(sa_text("UPDATE pedidos SET ultima_categoria='' WHERE ultima_categoria IS NULL"))
        db.execute(sa_text("UPDATE pedidos SET ultimos_filtros='' WHERE ultimos_filtros IS NULL"))
        db.execute(sa_text("UPDATE pedidos SET sugeridos_json='[]' WHERE sugeridos_json IS NULL OR TRIM(sugeridos_json)=''"))
        db.execute(sa_text("UPDATE pedidos SET ctx_json='{}' WHERE ctx_json IS NULL OR TRIM(ctx_json)=''"))
        db.execute(sa_text("UPDATE pedidos SET carrito_json='[]' WHERE carrito_json IS NULL OR TRIM(carrito_json)=''"))
        db.execute(sa_text("UPDATE pedidos SET preferencias_json='{}' WHERE preferencias_json IS NULL OR TRIM(preferencias_json)=''"))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

_normalize_nulls()

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
        db.execute(
            sa_text("UPDATE pedidos SET filtros = :f WHERE session_id = :sid"),
            {"f": json.dumps(filtro, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def get_user_filter(db: Session, session_id: str) -> Optional[dict]:
    try:
        row = db.execute(
            sa_text("SELECT filtros FROM pedidos WHERE session_id = :sid"),
            {"sid": session_id},
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None

def _get_sugeridos_urls(db: Session, session_id: str) -> List[str]:
    try:
        row = db.execute(
            sa_text("SELECT sugeridos FROM pedidos WHERE session_id=:sid"),
            {"sid": session_id},
        ).fetchone()
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
    """Guarda la lista completa de sugeridos como JSON en `sugeridos_json`."""
    try:
        db.execute(
            sa_text("UPDATE pedidos SET sugeridos_json=:j WHERE session_id=:sid"),
            {"j": json.dumps(lista, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def _get_sugeridos_list(db: Session, session_id: str) -> List[dict]:
    try:
        row = db.execute(
            sa_text("SELECT sugeridos_json FROM pedidos WHERE session_id=:sid"),
            {"sid": session_id},
        ).fetchone()
    except Exception:
        return []
    try:
        return json.loads(row[0]) if row and row[0] else []
    except Exception:
        return []

def _get_ultima_cat_filters(db: Session, session_id: str):
    try:
        row = db.execute(
            sa_text("SELECT ultima_categoria, ultimos_filtros FROM pedidos WHERE session_id=:sid"),
            {"sid": session_id}
        ).fetchone()
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
            row = db.execute(
                sa_text("SELECT ctx_json FROM pedidos WHERE session_id=:sid"),
                {"sid": sid},
            ).fetchone()
        finally:
            db.close()
        raw = row[0] if row and row[0] else "{}"
        return _safe_json_load(raw, {})
    except Exception:
        return {}

def _ctx_save(db: Session, session_id: str, ctx: dict):
    try:
        db.execute(
            sa_text("UPDATE pedidos SET ctx_json=:j WHERE session_id=:sid"),
            {"j": json.dumps(ctx, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def _remember_list(db: Session, session_id: str, cat: str, filtros: dict, productos: List[dict]):
    """Guarda: categoría/filtros + lista COMPLETA (con SKU) en `sugeridos_json` y en ctx."""
    try:
        db.execute(
            sa_text("UPDATE pedidos SET ultima_categoria=:c, ultimos_filtros=:f, sugeridos_json=:s WHERE session_id=:sid"),
            {
                "c": cat or "",
                "f": json.dumps(filtros, ensure_ascii=False),
                "s": json.dumps(productos, ensure_ascii=False),
                "sid": session_id,
            },
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

# ---------- Carrito y preferencias ----------
def _carrito_load(pedido) -> list:
    try:
        sid = pedido.session_id
        db = SessionLocal()
        try:
            row = db.execute(
                sa_text("SELECT carrito_json FROM pedidos WHERE session_id=:sid"),
                {"sid": sid},
            ).fetchone()
        finally:
            db.close()
        raw = row[0] if row and row[0] else "[]"
        data = _safe_json_load(raw, [])
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _carrito_save(db: Session, session_id: str, carrito: list):
    try:
        db.execute(
            sa_text("UPDATE pedidos SET carrito_json=:j WHERE session_id=:sid"),
            {"j": json.dumps(carrito, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def _prefs_load(pedido) -> dict:
    try:
        sid = pedido.session_id
        db = SessionLocal()
        try:
            row = db.execute(
                sa_text("SELECT preferencias_json FROM pedidos WHERE session_id=:sid"),
                {"sid": sid},
            ).fetchone()
        finally:
            db.close()
        raw = row[0] if row and row[0] else "{}"
        data = _safe_json_load(raw, {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _prefs_save(db: Session, session_id: str, prefs: dict):
    try:
        db.execute(
            sa_text("UPDATE pedidos SET preferencias_json=:j WHERE session_id=:sid"),
            {"j": json.dumps(prefs, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def _cart_add(carrito: list, sku: str, nombre: str, categoria: str,
              talla: str = None, color: str = None, cantidad: int = 1,
              precio_unitario: float = 0.0):
    for item in carrito:
        if item["sku"] == sku and item.get("talla") == talla and item.get("color") == color:
            item["cantidad"] = max(1, int(item.get("cantidad", 1))) + max(1, int(cantidad))
            return carrito
    carrito.append({
        "sku": sku, "nombre": nombre, "categoria": categoria,
        "talla": talla, "color": color, "cantidad": max(1, int(cantidad)),
        "precio_unitario": float(precio_unitario)
    })
    return carrito

def _cart_update_qty(carrito: list, sku: str, talla: str = None, color: str = None, cantidad: int = 1):
    for item in carrito:
        if item["sku"] == sku and item.get("talla") == talla and item.get("color") == color:
            item["cantidad"] = max(1, int(cantidad))
            return carrito
    return carrito

def _cart_remove(carrito: list, sku: str, talla: str = None, color: str = None):
    return [i for i in carrito if not (i["sku"] == sku and i.get("talla") == talla and i.get("color") == color)]

def _cart_total(carrito: list) -> float:
    return sum(float(i.get("precio_unitario", 0.0)) * int(i.get("cantidad", 1)) for i in carrito)

def _cart_summary_lines(carrito: list) -> List[str]:
    if not carrito:
        return ["Tu carrito está vacío."]
    lines = []
    for i, it in enumerate(carrito, 1):
        precio = _fmt_cop(it.get('precio_unitario', 0))
        qty = int(it.get("cantidad", 1))
        tail = " ".join([x for x in [(it.get("color") or ""), (it.get("talla") or "")] if x]).strip()
        tail = f" {tail}" if tail else ""
        lines.append(f"{i}. {it['nombre']} ({it['sku']}){tail} x{qty} – {precio} c/u")
    lines.append(f"\nTotal: {_fmt_cop(_cart_total(carrito))}")
    return lines

#  Prompt maestro
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

def depurar_pedidos_expirados(db: Session):
    umbral = datetime.now(timezone.utc) - timedelta(minutes=TIMEOUT_MIN)
    candidatos = db.query(Pedido).filter(Pedido.estado == "pendiente").all()
    tocados = 0
    for p in candidatos:
        try:
            if _to_utc(p.last_activity) < umbral:
                p.estado = "expirado"
                tocados += 1
        except Exception:
            continue
    if tocados:
        db.commit()

ALLOWED_CAMPOS = {
    "producto", "talla", "cantidad", "metodo_entrega", "direccion",
    "punto_venta", "metodo_pago", "estado", "nombre_cliente", "telefono",
    "email", "ciudad", "precio_unitario", "subtotal"
}

def _clean_json(texto: str) -> dict:
    try:
        start, end = texto.find("{"), texto.rfind("}")
        if start == -1 or end == -1:
            raise ValueError
        return json.loads(texto[start:end + 1])
    except Exception:
        return {
            "campos": {},
            "respuesta": "Puedo continuar con tu compra. ¿Quieres que agregue el producto que te gustó al carrito o prefieres ver el carrito primero?",
        }

def _gen_numero_confirmacion(prefix="CAS"):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    suf = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{ts}-{suf}"

def _pedido_missing_fields(pedido) -> list:
    faltan = []
    if not getattr(pedido, "nombre_cliente", None):
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

async def procesar_conversacion_llm(pedido, texto_usuario: str):
    if client is None:
        # Fallback sin LLM
        carrito = _carrito_load(pedido)
        lineas = _cart_summary_lines(carrito)
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

    sug = sugerir_productos(texto_usuario, limite=3)
    if isinstance(sug, dict):
        productos = (sug.get("productos") or [])[:3]
        mensaje = sug.get("mensaje")

    if not productos:
        cat, _ = detectar_categoria(texto_usuario)
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

    carrito = _carrito_load(pedido)
    prefs = _prefs_load(pedido)
    carrito_resumen = "\n".join(_cart_summary_lines(carrito))
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

    print("[DBG] texto_usuario:", texto_usuario)
    prods = extras.get("productos_disponibles") or []
    print("[DBG] productos_disponibles?:", bool(prods), "n =", len(prods))
    if prods:
        print("[DBG] primer producto:", prods[0].get("nombre"), prods[0].get("url"))
    else:
        print("[DBG] NO HAY PRODUCTOS DISPONIBLES")

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
        print("[DBG] respuesta LLM (raw):", raw)
        data = json.loads(raw)

        # 🔧 Normaliza tallas dentro de acciones cache_list
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

        print("[DBG] json parsed:", data)
        return data
    except Exception as e:
        import traceback
        print("[LLM_ERR]", repr(e))
        traceback.print_exc()
        print("RAW LLM:", raw if 'raw' in locals() else "")
        data = _clean_json(raw if 'raw' in locals() else "{}")
        raw_campos = data.get("campos", {})
        if not isinstance(raw_campos, dict):
            raw_campos = {}
        _ = {k: v for k, v in raw_campos.items() if k in ALLOWED_CAMPOS}
        return data

def _formatear_sugerencias(lista: List[dict]) -> str:
    lines = []
    for i, p in enumerate(lista[:3], start=1):
        precio = _fmt_cop(p.get('precio', 0))
        lines.append(f"{i}. {p['nombre']} - {precio} - {p['url']}")
    return "Aquí tienes algunas opciones:\n" + "\n".join(lines)

# --- Normalizadores y Action Protocol ---

def _normalize_llm_actions(acciones):
    norm = []
    for a in (acciones or []):
        if not isinstance(a, dict):
            continue
        if "tipo" in a and "args" in a:
            if a.get("tipo") == "cache_list":
                prods = (a.get("args") or {}).get("productos") or []
                for p in prods:
                    if isinstance(p, dict) and "tallas_disponibles" in p:
                        p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
            norm.append(a)
            continue
        for key in ("cache_list","add_item","update_qty","remove_item","show_cart","finalize_order","remember_pref"):
            if key in a:
                args = a.get(key) or {}
                if key == "cache_list":
                    prods = (args or {}).get("productos") or []
                    for p in prods:
                        if isinstance(p, dict) and "tallas_disponibles" in p:
                            p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                norm.append({"tipo": key, "args": args})
                break
    return norm

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
        carrito = _carrito_load(pedido)
        return {"response": "\n".join(_cart_summary_lines(carrito))}

    if action == "ASK_VARIANT":
        ref = payload.get("product_ref")
        prod = _resolve_product_ref(db, session_id, ref) if ref else None
        if prod:
            # Guarda el pendiente en el contexto
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
        carrito = _carrito_load(pedido)
        sku = str(payload.get("product_id") or payload.get("sku") or "")
        talla = payload.get("size")
        color = payload.get("color")
        carrito = _cart_remove(carrito, sku, talla, color)
        _carrito_save(db, session_id, carrito)
        try:
            actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
        except Exception:
            pass
        return {"response": "\n".join(_cart_summary_lines(carrito))}

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
        # valida que la talla exista
        if tallas and size and size.upper() not in tallas:
            return {"response": f"Para «{prod.get('nombre','Producto')}» tengo {', '.join(tallas)}. ¿Quieres elegir una de esas tallas?"}

        carrito = _carrito_load(pedido)
        carrito = _cart_add(
            carrito,
            sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
            nombre=prod.get("nombre","Producto"),
            categoria=prod.get("categoria",""),
            talla=size.upper() if isinstance(size, str) else size,
            color=payload.get("color") or prod.get("color"),
            cantidad=int(payload.get("qty") or 1),
            precio_unitario=float(prod.get("precio", 0.0)),
        )
        _carrito_save(db, session_id, carrito)
        try:
            actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
        except Exception:
            pass
        return {"response": "Agregado al carrito ✅\n\n" + "\n".join(_cart_summary_lines(carrito))}

    return None

#  Endpoint conversación
@app.post("/mensaje-whatsapp")
async def mensaje_whatsapp(user_input: UserMessage, session_id: str, db: Session = Depends(get_db)):
    depurar_pedidos_expirados(db)

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
            "ctx_json": "{}",            # ✅ asegurado
            "ultima_categoria": "",      # ✅ asegurado
            "ultimos_filtros": "",       # ✅ asegurado
            "sugeridos_json": "[]",      # ✅ asegurado
            "carrito_json": "[]",
            "preferencias_json": "{}",
            "numero_confirmacion": "",
        })
        pedido = obtener_pedido_por_sesion(db, session_id)

    last_act_utc = _to_utc(getattr(pedido, "last_activity", None))
    tiempo_inactivo = ahora - last_act_utc

    user_text = user_input.message.strip().lower()
    actualizar_pedido_por_sesion(db, session_id, "last_activity", ahora)

    # --------- Detectar filtros del mensaje (con validación de talla) ---------
    filtros_detectados = {}
    m_color = COLOR_RE.search(user_text)
    if m_color:
        filtros_detectados["color"] = m_color.group(1).lower()

    m_talla_tok = TALLA_TOKEN_RE.search(user_text)
    if m_talla_tok:
        talla_val = (m_talla_tok.group(1) or "").upper()
        if talla_val in TALLAS_VALIDAS:        # ✅ solo tallas reales
            filtros_detectados["talla"] = talla_val

    m_manga = MANGA_RE.search(user_text)
    if m_manga:
        filtros_detectados["manga"] = m_manga.group(1).lower()

    m_uso = USO_RE.search(user_text)
    if m_uso:
        filtros_detectados["uso"] = m_uso.group(1).lower()

    if filtros_detectados:
        print("[DBG] Guardando filtros:", filtros_detectados)
        set_user_filter(db, session_id, filtros_detectados)

    # ✅ Si llegó talla y hay un producto pendiente (ASK_VARIANT), agréguelo al carrito y limpia pending
    if "talla" in filtros_detectados:
        ctx = _ctx_load(pedido)
        pv = ctx.get("pending_variant")
        if pv:
            prod = _resolve_product_ref(db, session_id, pv.get("ref") or pv.get("sku"))
            if prod:
                tallas = _clean_tallas(prod.get("tallas_disponibles") or [])
                if tallas and filtros_detectados["talla"] not in tallas:
                    return {"response": f"Para «{prod.get('nombre','Producto')}» tengo {', '.join(tallas)}. ¿Quieres elegir una de esas tallas?"}
                carrito = _carrito_load(pedido)
                carrito = _cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=filtros_detectados["talla"],
                    color=prod.get("color"),
                    cantidad=int(pv.get("qty") or 1),
                    precio_unitario=float(prod.get("precio", 0.0)),
                )
                _carrito_save(db, session_id, carrito)
                try:
                    actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
                except Exception:
                    pass
                ctx.pop("pending_variant", None)
                _ctx_save(db, session_id, ctx)
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(_cart_summary_lines(carrito))}

    # ✅ Cantidad pendiente tras seleccionar un producto sin tallas
    ctx_qty = _ctx_load(pedido)
    awaiting = ctx_qty.get("awaiting_qty")
    if awaiting:
        qty = _extract_qty(user_text)
        if qty:
            ref = str(awaiting.get("ref") or "")
            prod = _resolve_product_ref(db, session_id, ref) or _resolve_product_ref(db, session_id, awaiting.get("sku") or "")
            if prod:
                carrito = _carrito_load(pedido)
                carrito = _cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=None,
                    color=prod.get("color"),
                    cantidad=int(qty),
                    precio_unitario=float(prod.get("precio", 0.0) or 0.0),
                )
                _carrito_save(db, session_id, carrito)
                try:
                    actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
                except Exception:
                    pass
                ctx_qty.pop("awaiting_qty", None)
                _ctx_save(db, session_id, ctx_qty)
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(_cart_summary_lines(carrito))}
        return {"response": "¿Cuántas unidades deseas? (por ejemplo: 1, 2 o 3)"}

    # ✅ Fast-path talla sola
    m_talla_solo = TALLA_TOKEN_RE.search(user_text)
    if m_talla_solo and not MOSTRAR_RE.search(user_text) and not SELECCION_RE.search(user_text) and not ORDINAL_RE.search(user_text):
        talla_elegida = (m_talla_solo.group(1) or "").upper()
        if talla_elegida in TALLAS_VALIDAS:
            ctx = _ctx_load(pedido)
            last = (ctx.get("selecciones") or [])[-1] if ctx.get("selecciones") else None
            if last:
                lista = _get_sugeridos_list(db, session_id)
                prod = next((p for p in (lista or []) if p.get("url") == last.get("url") or p.get("nombre") == last.get("nombre")), None) or last
                tallas = _clean_tallas(prod.get("tallas_disponibles"))
                if tallas and talla_elegida not in tallas:
                    return {"response": f"Para «{prod.get('nombre','Producto')}» tengo {', '.join(tallas)}. ¿Quieres elegir una de esas tallas?"}
                carrito = _carrito_load(pedido)
                carrito = _cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=talla_elegida,
                    color=prod.get("color"),
                    cantidad=1,
                    precio_unitario=float(prod.get("precio", 0.0))
                )
                _carrito_save(db, session_id, carrito)
                try:
                    actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
                except Exception:
                    pass
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(_cart_summary_lines(carrito))}

    # ✅ Confirmación corta basada en contexto
    ctx_tmp = _ctx_load(pedido)
    if ctx_tmp.get("awaiting_confirmation"):
        if re.search(r'^(s[ií]|ok|dale|listo|de acuerdo|est(a|á)\s*bien|as(i|í)\s*est(a|á)\s*bien)\b', user_text, re.I):
            actualizar_pedido_por_sesion(db, session_id, "estado", "confirmado")
            # genera número si falta
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
            except Exception:
                pass
            try:
                mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
                await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
            except Exception:
                pass

            carrito = _carrito_load(pedido_actualizado)
            resumen = "\n".join(_cart_summary_lines(carrito))
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

    # ✅ Manejo de pago/confirmación (router híbrido)
    resp_pago = await procesar_mensaje_usuario(user_text, db, session_id, pedido)
    if resp_pago:
        return resp_pago

    # ✅ Reinicio por inactividad
    if tiempo_inactivo.total_seconds() / 60 > TIMEOUT_MIN:
        db.query(Pedido).filter(Pedido.session_id == session_id).delete()
        db.commit()
        crear_pedido(
            db,
            {
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
            },
        )
        actualizar_pedido_por_sesion(db, session_id, "saludo_enviado", 1)
        return {"response": SALUDO_BASE}

    # Autoregistro de teléfono desde session_id si aplica
    if not getattr(pedido, "telefono", None) and session_id.startswith("cliente_"):
        telefono_cliente = session_id.replace("cliente_", "")
        actualizar_pedido_por_sesion(db, session_id, "telefono", telefono_cliente)

    if pedido and pedido.estado == "cancelado":
        if SALUDO_RE.match(user_text):
            db.query(Pedido).filter(Pedido.session_id == session_id).delete()
            db.commit()
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
                "ctx_json": "{}",            # ✅
                "ultima_categoria": "",      # ✅
                "ultimos_filtros": "",       # ✅
                "sugeridos_json": "[]",      # ✅
                "carrito_json": "[]",
                "preferencias_json": "{}",
                "numero_confirmacion": "",
            })
            return {"response": SALUDO_BASE}
        return {"response": "No tienes ningún pedido activo en este momento. Escribe ‘hola’ cuando quieras comenzar una nueva compra."}

    if detectar_intencion_atencion(user_text):
        mensaje_alerta = generar_mensaje_atencion_humana(pedido)
        await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
        return {"response": "Entendido, ya te pongo en contacto con uno de nuestros asesores. Te responderán personalmente en breve para ayudarte con lo que necesitas."}

    if any(neg in user_text for neg in ["ya no quiero", "cancelar pedido", "no deseo", "me arrepentí", "me arrepenti"]):
        producto_cancelado = pedido.producto or "el pedido actual"
        actualizar_pedido_por_sesion(db, session_id, "estado", "cancelado")
        actualizar_pedido_por_sesion(db, session_id, "producto", "")
        actualizar_pedido_por_sesion(db, session_id, "talla", "")
        actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "")
        actualizar_pedido_por_sesion(db, session_id, "punto_venta", "")
        return {"response": f"Entiendo, he cancelado {producto_cancelado}. ¿Te gustaría ver otra prenda o necesitas ayuda con algo más?"}

    if SALUDO_RE.match(user_text):
        if _get_saludo_enviado(db, session_id) == 0:
            actualizar_pedido_por_sesion(db, session_id, "saludo_enviado", 1)
            return {"response": SALUDO_BASE}
        return {"response": "¡Hola! ¿Qué te gustaría ver hoy: camisas, jeans, pantalones o suéteres?"}

    if DOMICILIO_RE.search(user_text):
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "domicilio")
        if not getattr(pedido, "datos_personales_advertidos", False):
            actualizar_pedido_por_sesion(db, session_id, "datos_personales_advertidos", True)
            return {
                "response": (
                    "Antes de continuar, ten en cuenta que tus datos personales serán tratados "
                    "bajo nuestra política de tratamiento de datos, que puedes consultar aquí:\n"
                    "https://cassany.co/tratamiento-de-datos-personales/\n\n"
                    "Ahora, ¿podrías proporcionarme tu dirección y ciudad para el envío?"
                )
            }
        return {"response": "Perfecto, por favor indícame tu dirección y ciudad para el envío."}

    if RECOGER_RE.search(user_text):
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "recoger_en_tienda")
        tiendas = "\n".join(PUNTOS_VENTA)
        return {"response": f"Por favor, confirma en cuál de nuestras tiendas deseas recoger tu pedido:\n{tiendas}"}

    if SMALLTALK_RE.search(user_text):
        cats = ", ".join(CATEGORIAS_RESUMEN[:4]) + "…"
        return {"response": f"¡Con gusto! ¿Te muestro algo hoy? Tenemos {cats} ¿Qué prefieres ver primero?"}

    if OFFTOPIC_RE.search(user_text):
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {"response": "Somos CASSANY, una marca de ropa para hombre. Trabajamos estas categorías:\n" + f"{cats}\n\n" + "¿Te muestro camisas o prefieres otra categoría?"}

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

    if CARRO_RE.search(user_text):
        carrito = _carrito_load(pedido)
        lineas = _cart_summary_lines(carrito)
        return {"response": "\n".join(lineas)}

       m_fotos = FOTOS_RE.search(user_text)
    if m_fotos:
        if _tiene_atributos_especificos(user_text):
            pass
        else:
            cat_txt = m_fotos.group(2).strip()
            cat, _ = detectar_categoria(cat_txt)
            consulta = cat or cat_txt

            urls_previas = _get_sugeridos_urls(db, session_id)
            res = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)
            productos = res.get("productos", [])

            if productos:
                filtros = detectar_atributos(cat_txt) or {}
                for p in productos:
                    if isinstance(p, dict) and "tallas_disponibles" in p:
                        p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                _remember_list(db, session_id, cat or "", filtros, productos)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if p.get("url")])
                return {"response": _formatear_sugerencias(productos[:3])}

            cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
            return {"response": f"No hay stock para «{cat_txt}» en este momento. ¿Te muestro algo de:\n{cats}"}

    if MOSTRAR_RE.search(user_text):
        if _tiene_atributos_especificos(user_text):
            pass
        else:
            ultima_cat, _ = _get_ultima_cat_filters(db, session_id)
            cat, _ = detectar_categoria(user_text)
            consulta = cat or ultima_cat
            if consulta:
                urls_previas = _get_sugeridos_urls(db, session_id)
                res = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)
                productos = res.get("productos", [])
                if productos:
                    for p in productos:
                        if isinstance(p, dict) and "tallas_disponibles" in p:
                            p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                    _set_sugeridos_list(db, session_id, productos)
                    _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if p.get("url")])
                    return {"response": _formatear_sugerencias(productos[:3])}
            cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
            return {"response": f"¿Qué te muestro primero?\n{cats}"}

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
                return {"response": _formatear_sugerencias(restantes)}
            else:
                return {"response": "Ya te mostré todas las opciones disponibles por ahora. ¿Quieres buscar algo diferente?"}

        ultima_cat, ult_filtros = _get_ultima_cat_filters(db, session_id)
        if not ultima_cat:
            ultima_cat, _ = detectar_categoria(user_text)

        if ultima_cat:
            partes = [ultima_cat]
            if isinstance(ult_filtros, dict):
                if ult_filtros.get("subtipo") == "guayabera":
                    partes.append("guayabera")
                if ult_filtros.get("manga") in ("corta", "larga"):
                    partes.append(f"manga {ult_filtros['manga']}")
                if ult_filtros.get("color"):
                    partes.append(ult_filtros["color"])
            consulta = " ".join(partes)
            urls_previas = _get_sugeridos_urls(db, session_id)

            res = sugerir_productos(consulta, limite=3, excluir_urls=urls_previas)
            productos = res.get("productos", [])

            if not productos:
                res2 = sugerir_productos(ultima_cat, limite=3, excluir_urls=urls_previas)
                productos = res2.get("productos", [])

            if productos:
                for p in productos:
                    if isinstance(p, dict) and "tallas_disponibles" in p:
                        p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                filtros_persist = ult_filtros if isinstance(ult_filtros, dict) and ult_filtros else detectar_atributos(user_text)
                _remember_list(db, session_id, ultima_cat, filtros_persist, productos)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if p.get("url")])
                return {"response": _formatear_sugerencias(productos)}
            else:
                msg = (res.get("mensaje") if isinstance(res, dict) else None) or \
                    f"No hay stock en la categoría «{ultima_cat}» en este momento."
                return {"response": msg + " ¿Te muestro algo similar?"}

        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {"response": f"No detecté ninguna categoría concreta en tu solicitud. ¿Te gustaría que te muestre opciones de:\n{cats}"}

    # ✅ Selección por ordinal (“la primera / segunda / tercera”)
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
                carrito = _carrito_load(pedido)
                carrito = _cart_add(
                    carrito,
                    sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                    nombre=prod.get("nombre","Producto"),
                    categoria=prod.get("categoria",""),
                    talla=None,
                    color=prod.get("color"),
                    cantidad=1,
                    precio_unitario=float(prod.get("precio", 0.0))
                )
                _carrito_save(db, session_id, carrito)
                actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
                return {"response": "Agregado al carrito ✅\n\n" + "\n".join(_cart_summary_lines(carrito))}
            if tallas:
                return {"response": f"Listo, seleccionaste la opción {idx0+1}. Tallas disponibles: {', '.join(tallas)}. ¿Cuál prefieres?"}
            ctx_set = _ctx_load(pedido)
            ctx_set["awaiting_qty"] = {
                "ref": idx0 + 1,
                "sku": prod.get("sku") or prod.get("url") or prod.get("nombre")
            }
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
            if 0 <= idx < len(lista):
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
                    carrito = _carrito_load(pedido)
                    carrito = _cart_add(
                        carrito,
                        sku=prod.get("sku") or prod.get("url") or prod.get("nombre","Producto"),
                        nombre=prod.get("nombre","Producto"),
                        categoria=prod.get("categoria",""),
                        talla=None,
                        color=prod.get("color"),
                        cantidad=1,
                        precio_unitario=float(prod.get("precio", 0.0))
                    )
                    _carrito_save(db, session_id, carrito)
                    try:
                        actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
                    except Exception:
                        pass
                    lineas = _cart_summary_lines(carrito)
                    return {"response": "Agregado al carrito ✅\n\n" + "\n".join(lineas)}

                if tallas:
                    return {"response": f"Listo, seleccionaste la opción {idx+1}. Tallas disponibles: {', '.join(tallas)}. ¿Cuál prefieres?"}
                ctx_set = _ctx_load(pedido)
                ctx_set["awaiting_qty"] = {
                    "ref": idx + 1,
                    "sku": prod.get("sku") or prod.get("url") or prod.get("nombre")
                }
                _ctx_save(db, session_id, ctx_set)
                return {"response": f"Listo, seleccionaste la opción {idx+1}. ¿Cuántas unidades deseas?"}
            if lista:
                return {"response": f"Por favor indícame un número entre 1 y {len(lista)} de la lista que te mostré."}

    if any(pat in user_text for pat in PATRONES_RECHAZO):
        urls_previas = _get_sugeridos_urls(db, session_id)
        res = sugerir_productos(user_text, limite=3, excluir_urls=urls_previas)
        productos = res.get("productos", [])

        if len(productos) < 3:
            cat_relajada, _ = detectar_categoria(user_text)
            if cat_relajada:
                res2 = sugerir_productos(cat_relajada, limite=3, excluir_urls=urls_previas)
                ya = {p["url"] for p in productos}
                productos += [p for p in res2.get("productos", []) if p["url"] not in ya]

        if productos:
            try:
                cat_local, _ = detectar_categoria(user_text)
                filtros = detectar_atributos(user_text)
                for p in productos:
                    if isinstance(p, dict) and "tallas_disponibles" in p:
                        p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                actualizar_pedido_por_sesion(db, session_id, "ultima_categoria", cat_local or "")
                actualizar_pedido_por_sesion(db, session_id, "ultimos_filtros", json.dumps(filtros, ensure_ascii=False))
                _set_sugeridos_list(db, session_id, productos)
            except Exception:
                pass

            _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if p.get("url")])
            return {"response": _formatear_sugerencias(productos)}
        else:
            msg = res.get("mensaje") or "No encontré opciones que cumplan lo que pides. ¿Te muestro algo similar?"
            return {"response": msg}

    # ======= LLM =======
    resultado = await procesar_conversacion_llm(pedido, user_text)

    # a) Soporte del Protocolo de Acciones (ADD_TO_CART / SHOW_CART / etc.)
    handled = _handle_action_protocol(resultado, db, session_id, pedido)
    if handled:
        return handled

    if not isinstance(resultado, dict):
        return {"response": "Disculpa, ocurrió un error procesando tu solicitud. ¿Te muestro opciones de camisas o jeans?"}

    # b) Normaliza acciones mal formadas y limpia tallas
    if "acciones" in resultado:
        resultado["acciones"] = _normalize_llm_actions(resultado["acciones"])

    acciones_llm = resultado.get("acciones", [])
    if not any((a.get("tipo") == "cache_list") for a in acciones_llm):
        productos_previos = _get_sugeridos_list(db, session_id)
        if not productos_previos:
            categoria_detectada, _ = detectar_categoria(user_text)
            filtros = detectar_atributos(user_text)
            partes = [categoria_detectada] if categoria_detectada else []
            if filtros.get("subtipo") == "guayabera":
                partes.append("guayabera")
            if filtros.get("manga") in ("corta", "larga"):
                partes.append(f"manga {filtros['manga']}")
            if filtros.get("color"):
                partes.append(f"color {filtros['color']}")
            if filtros.get("talla"):
                partes.append(f"talla {filtros['talla']}")
            if filtros.get("uso"):
                partes.append(filtros["uso"])

            consulta = " ".join(partes).strip()
            urls_previas = _get_sugeridos_urls(db, session_id)
            res_fallback = sugerir_productos(consulta or user_text, limite=12, excluir_urls=urls_previas)
            productos = res_fallback.get("productos", [])

            if productos:
                print("[DBG] Fallback: forzando guardado de productos (no hubo cache_list)")
                for p in productos:
                    if isinstance(p, dict) and "tallas_disponibles" in p:
                        p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                _remember_list(db, session_id, categoria_detectada or "", filtros or {}, productos)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if "url" in p])
                resultado["respuesta"] = _formatear_sugerencias(productos[:3]) + \
                    "\n¿Te muestro más opciones o agrego alguna al carrito?"

    acciones = resultado.get("acciones") or []
    if isinstance(acciones, list) and acciones:
        carrito = _carrito_load(pedido)
        prefs = _prefs_load(pedido)

        for act in acciones:
            try:
                t = (act.get("tipo") or "").strip()
                args = act.get("args") or {}
                if t == "add_item":
                    carrito = _cart_add(
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
                    carrito = _cart_update_qty(
                        carrito,
                        sku=args["sku"],
                        talla=args.get("talla"),
                        color=args.get("color"),
                        cantidad=int(args.get("cantidad", 1))
                    )
                elif t == "remove_item":
                    carrito = _cart_remove(
                        carrito,
                        sku=args["sku"],
                        talla=args.get("talla"),
                        color=args.get("color")
                    )
                elif t == "show_cart":
                    pass
                elif t == "remember_pref":
                    cat = args.get("categoria")
                    talla = args.get("talla")
                    color_fav = args.get("color_favorito")
                    prefs.setdefault("tallas_preferidas", {})
                    if cat and talla:
                        prefs["tallas_preferidas"][cat] = talla
                    if color_fav:
                        prefs["color_favorito"] = color_fav
                elif t == "finalize_order":
                    pass
                elif t == "cache_list":
                    productos = args.get("productos") or []
                    if isinstance(productos, list) and productos:
                        for p in productos:
                            if isinstance(p, dict) and "tallas_disponibles" in p:
                                p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                        try:
                            _set_sugeridos_list(db, session_id, productos)
                            _append_sugeridos_urls(
                                db, session_id,
                                [p.get("url") for p in productos if isinstance(p, dict) and p.get("url")]
                            )
                            if len(productos) <= 3:
                                ultima_cat, ult_filtros = _get_ultima_cat_filters(db, session_id)
                                if not ultima_cat:
                                    ultima_cat, _ = detectar_categoria(user_text)

                                partes = [ultima_cat] if ultima_cat else []
                                if isinstance(ult_filtros, dict):
                                    if ult_filtros.get("subtipo") == "guayabera":
                                        partes.append("guayabera")
                                    if ult_filtros.get("manga") in ("corta", "larga"):
                                        partes.append(f"manga {ult_filtros['manga']}")
                                    if ult_filtros.get("color"):
                                        partes.append(ult_filtros["color"])
                                    if ult_filtros.get("uso"):
                                        partes.append(ult_filtros["uso"])
                                    if ult_filtros.get("talla"):
                                        partes.append(f"talla {ult_filtros['talla']}")

                                consulta = " ".join([p for p in partes if p]).strip() or (user_text or "")
                                urls_previas = _get_sugeridos_urls(db, session_id)
                                res_plus = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)
                                extra = res_plus.get("productos", [])
                                if extra:
                                    for p in extra:
                                        if isinstance(p, dict) and "tallas_disponibles" in p:
                                            p["tallas_disponibles"] = _clean_tallas(p.get("tallas_disponibles"))
                                    ya = {p.get("url") for p in productos if isinstance(p, dict)}
                                    merged = productos + [e for e in extra if isinstance(e, dict) and e.get("url") not in ya]
                                    _set_sugeridos_list(db, session_id, merged)
                                    _append_sugeridos_urls(
                                        db, session_id,
                                        [p.get("url") for p in extra if isinstance(p, dict) and p.get("url")]
                                    )
                                    print(f"[DBG] cache_list top-up: {len(productos)} -> {len(merged)} items")
                        except Exception:
                            pass
            except Exception:
                continue

        _carrito_save(db, session_id, carrito)
        _prefs_save(db, session_id, prefs)

        try:
            actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
        except Exception:
            pass

        if any((a.get("tipo") == "show_cart") for a in acciones):
            lineas = _cart_summary_lines(carrito)
            resultado["respuesta"] = ( "\n".join(lineas) + "\n\n" + (resultado.get("respuesta") or "") ).strip()

    # mapeo nombre_completo → nombre_cliente antes de guardar
    campos_dict = resultado.get("campos", {}) or {}
    if "nombre_completo" in campos_dict and "nombre_cliente" not in campos_dict:
        campos_dict["nombre_cliente"] = campos_dict.pop("nombre_completo")

    if isinstance(campos_dict, dict):
        if "producto" in campos_dict:
            actualizar_pedido_por_sesion(db, session_id, "talla", "")
            actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
        for campo, val in campos_dict.items():
            if campo in ALLOWED_CAMPOS:
                actualizar_pedido_por_sesion(db, session_id, campo, val)
        if ("talla" in campos_dict) or ("cantidad" in campos_dict):
            _update_last_selection_from_pedido(db, session_id)

    # Preguntas específicas según campos recién seteados
    if campos_dict.get("metodo_entrega") == "recoger_en_tienda" and not campos_dict.get("punto_venta"):
        tiendas = "\n".join(PUNTOS_VENTA)
        return {"response": f"Por favor, confirma en cuál de nuestras tiendas deseas recoger tu pedido:\n{tiendas}"}

    if campos_dict.get("metodo_entrega") == "domicilio":
        if not getattr(pedido, "datos_personales_advertidos", False):
            actualizar_pedido_por_sesion(db, session_id, "datos_personales_advertidos", True)
            return {
                "response": (
                    "Antes de continuar, ten en cuenta que tus datos personales serán tratados "
                    "bajo nuestra política de tratamiento de datos, que puedes consultar aquí:\n"
                    "https://cassany.co/tratamiento-de-datos-personales/\n\n"
                    "Ahora, ¿podrías proporcionarme tu dirección y ciudad para el envío?"
                )
            }
        return {"response": "Perfecto, por favor indícame tu dirección y ciudad para el envío."}

    if campos_dict.get("direccion") and campos_dict.get("ciudad"):
        # Disparo oportunista a HubSpot (no bloqueante)
        try:
            pedido_tmp = obtener_pedido_por_sesion(db, session_id)
            enviar_pedido_a_hubspot(pedido_tmp)
        except Exception:
            pass
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
        except Exception:
            pass
        try:
            mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
            await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, mensaje_alerta)
        except Exception:
            pass

    # --- Verificador de faltantes críticos (pide 1 cosa a la vez) ---
    pedido_refresco = obtener_pedido_por_sesion(db, session_id)
    faltan = _pedido_missing_fields(pedido_refresco)
    if faltan:
        pregunta = _prompt_for_missing(pedido_refresco, faltan)
        if pregunta:
            base = resultado.get("respuesta") or ""
            sep = "\n\n" if base else ""
            return {"response": (base + sep + pregunta).strip()}

    return {"response": resultado.get("respuesta", "Disculpa, ocurrió un error.")}

# ------------------ Health / Webhook ------------------
@app.get("/")
def root():
    return {"ok": True, "service": "cassany", "build": APP_BUILD, "docs": "/docs"}

@app.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(400, "Token de verificación inválido.")

def _verify_wa_signature(raw_body: bytes, signature_256: str) -> bool:
    """
    Verifica la firma 'X-Hub-Signature-256' con el WA_APP_SECRET.
    """
    if not WA_APP_SECRET:
        return True  # si no hay secreto configurado, permitir (modo dev)
    if not signature_256:
        return False
    try:
        mac = hmac.new(WA_APP_SECRET.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256)
        expected = "sha256=" + mac.hexdigest()
        # tiempo-constante
        return hmac.compare_digest(expected, signature_256)
    except Exception:
        return False

@app.post("/webhook")
async def receive_whatsapp_message(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, convert_underscores=False),
):
    raw = await request.body()
    if not _verify_wa_signature(raw, x_hub_signature_256 or ""):
        raise HTTPException(403, "Firma inválida")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        data = {}
    print("📥 MENSAJE RECIBIDO DE WHATSAPP:\n", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {}) or {}
            if value.get("statuses"):
                continue  # ignorar acuses de entrega

            for msg in value.get("messages", []):
                msg_type = msg.get("type")
                if msg_type not in ("text", "interactive"):
                    continue

                num = msg.get("from")
                if msg_type == "interactive":
                    inter = msg.get("interactive") or {}
                    txt = (inter.get("button_reply") or {}).get("title") or (inter.get("list_reply") or {}).get("title") or ""
                else:
                    txt = (msg.get("text") or {}).get("body", "")
                msg_id = msg.get("id")

                if not (num and txt and msg_id):
                    continue

                session_id = f"cliente_{num}"
                db = SessionLocal()

                try:
                    last = _get_last_msg_id(db, session_id)
                    if last == msg_id:
                        # ya procesado este mensaje
                        continue

                    print(f"🧪 Texto recibido: {txt}")
                    res = await mensaje_whatsapp(UserMessage(message=txt), session_id=session_id, db=db)
                    # marca como procesado ANTES de enviar para evitar reprocesos en reintentos
                    actualizar_pedido_por_sesion(db, session_id, "last_msg_id", msg_id)
                    await enviar_mensaje_whatsapp(num, res["response"])
                finally:
                    db.close()
    return {"status": "received"}

async def enviar_mensaje_whatsapp(numero: str, mensaje: str):
    url = f"https://graph.facebook.com/{WA_GRAPH_API_VER}/{WHATSAPP_PHONE_NUMBER}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": mensaje}}
    try:
        async with httpx.AsyncClient(timeout=10) as client_http:
            r = await client_http.post(url, headers=headers, json=payload)
            r.raise_for_status()
        print("✅ Mensaje enviado a WhatsApp")
    except Exception as exc:
        print("❌ Error envío WhatsApp:", exc)
        print("🚀 Enviando mensaje a:", numero)
        print("📨 Contenido:", mensaje)

@app.get("/__version")
def version():
    return {"build": APP_BUILD}

@app.get("/test-whatsapp")
async def test_whatsapp():
    await enviar_mensaje_whatsapp(ALERTA_WHATSAPP, "🚀 Token nuevo activo. Esta es una prueba en vivo.")
    return {"status": "sent"}

# ---------- INIT ----------
init_db()

