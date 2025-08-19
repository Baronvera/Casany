APP_BUILD = "build_03"

import os
import json
import re
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, List
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from openai import OpenAI
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes_agent import router as agent_router

#  Configuración y cliente OpenAI
load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT_ID")  # opcional
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY en el entorno (.env).")

openai_client_kwargs = {
    "api_key": OPENAI_API_KEY,
    "timeout": 30,        # segundos
    "max_retries": 2,     # reintentos ante errores transitorios
}
if OPENAI_PROJECT_ID:
    openai_client_kwargs["project"] = OPENAI_PROJECT_ID

client = OpenAI(**openai_client_kwargs)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
try:
    TIMEOUT_MIN = max(1, int(os.getenv("SESSION_TIMEOUT_MIN", "60")))
except ValueError:
    TIMEOUT_MIN = 60

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # 👈 Captura solo ImportError, no Exception genérico
    from backports.zoneinfo import ZoneInfo  # Python 3.8

# --- Zona horaria local (Bogotá) y helpers de tiempo/JSON ---
LOCAL_TZ = ZoneInfo("America/Bogota")
UTC = timezone.utc

def now_utc() -> datetime:
    """Datetime consciente de zona, en UTC."""
    return datetime.now(UTC)

def parse_db_ts(val) -> datetime:
    """
    Normaliza timestamps provenientes de BD o strings.
    Regla: si es naive, se asume UTC (SQLite CURRENT_TIMESTAMP es UTC).
    """
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
    """
    Serializa a 'YYYY-MM-DD HH:MM:SS' en UTC para guardar en BD (columna DATETIME).
    """
    if not isinstance(dt, datetime):
        dt = now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

def _to_utc(dt: datetime) -> datetime:
    """
    Compat: mantén el nombre usado en el resto del código. Trata naive como UTC (no como hora local).
    """
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
# (dejamos SOLO esta versión que entiende “ver más”)
MAS_OPCIONES_RE = re.compile(r'\b(más opciones|mas opciones|muéstrame más|muestrame mas|ver más|ver mas)\b', re.I)
DOMICILIO_RE = re.compile(r'\b(a\s*domicilio|env[ií]o\s*a\s*domicilio|domicilio)\b', re.I)
RECOGER_RE  = re.compile(r'\b(recoger(?:lo)?\s+en\s+(tienda|sucursal)|retiro\s+en\s+tienda)\b', re.I)
SELECCION_RE = re.compile(r'(?:opci(?:o|ó)n\s*(\d+))|(?:\bla\s*(\d+)\b)|(?:n[uú]mero\s*(\d+))|^(?:\s*)(\d+)(?:\s*)$', re.I)
# Pedidos de agregar al carrito
ADD_RE = re.compile(r'\b(agrega|agregar|añade|añadir|mete|pon(?:er)?|suma|agregalo|agregá|agregame)\b', re.I)
# Preguntas generales / off-topic que debemos redirigir a venta
OFFTOPIC_RE = re.compile(
    r"(qué\s+vend[eé]n?|que\s+vend[eé]n?|qué\s+es\s+cassany|qu[eé]\s+es\s+cassany|"
    r"d[oó]nde\s+est[aá]n|ubicaci[oó]n|horarios?|qu[ií]en(es)?\s+son|historia|"
    r"c[oó]mo\s+funciona|pol[ií]tica(s)?\s+(de\s+)?(cambio|devoluci[oó]n|datos)|"
    r"p[óo]liza|env[ií]os?\s*(nacionales|a\s+d[oó]nde)?|m[ée]todos?\s+de\s+pago)", re.I)
# Small-talk que no debe disparar el LLM ni romper el flujo
SMALLTALK_RE = re.compile(
    r"^(gracias|muchas gracias|ok|dale|listo|perfecto|bien|super|s[uú]per|genial|jaja+|jeje+|"
    r"vale|de acuerdo|entendido|thanks|okey)\W*$", re.I)
# Intención de descubrimiento/indecisión (no sabe qué comprar)
DISCOVERY_RE = re.compile(
    r"(no\s*s[eé]\s*qu[eé]\s*comprar|qu[eé]\s+me\s+(recomiendas|sugieres)|recomi[eé]ndame|"
    r"me\s+ayudas?\s+a\s+elegir|m(u|ú)estrame\s+opciones|quiero\s+ver\s+opciones|"
    r"sugerencias|recomendaci[oó]n)", re.I)
CARRO_RE   = re.compile(r'\b(carrito|mi carrito|ver carrito|ver el carrito|carro|mi pedido|resumen del pedido)\b', re.I)
MOSTRAR_RE = re.compile(r'\b(mu[eé]strame|muestrame|mostrarme|puedes mostrarme|puede mostrarme|podr[ií]as? mostrarme|quiero ver|ens[eñ]a(?:me)?)\b', re.I)
FOTOS_RE   = re.compile(r'\b(fotos?|im[aá]genes?)\s+de\s+([a-záéíóúñü\s]+)\b', re.I)
TALLA_RE = re.compile(r'\btalla\b|\b(XXL|XL|XS|S|M|L)\b', re.I)
USO_RE = re.compile(r'\b(oficina|formal|casual|evento|trabajo)\b', re.I)
MANGA_RE = re.compile(r'\bmanga\s+(corta|larga)\b', re.I)
COLOR_RE = re.compile(
    r'\b(blanco|blanca|negro|negra|azul|azules|beige|gris|rojo|verde|café|marr[oó]n|vinotinto|mostaza|'
    r'crema|turquesa|celeste|lila|morado|rosa|rosado|amarillo|naranja)\b', re.I
)
PAGO_RE = re.compile(
    r'(pagar|pago|quiero pagar|voy a pagar|prefiero pagar|el pago|pagaremos|pagare).*(transferencia|bancolombia|davivienda|payu|pago en tienda|efectivo|contraentrega)'
    r'|(transferencia|bancolombia|davivienda|payu|pago en tienda|efectivo|contraentrega).*(pagar|pago|quiero|voy|prefiero|pagaremos|pagare)',
    re.I
)
CONFIRM_RE = re.compile(
    r'(confirmar|confirmo|finalizar|cerrar|terminar|listo|realizar).*(pedido|compra|orden)'
    r'|(pedido|compra|orden).*(confirmar|confirmo|finalizar|cerrar|terminar|listo|realizar)',
    re.I
)

async def procesar_mensaje_usuario(text: str, db, session_id, pedido):
    # 👉 Pago / Confirmación (router híbrido: regex -> LLM clasificador)
    # 1) Intento barato (regex)
    pago_match = PAGO_RE.search(text)
    confirm_match = CONFIRM_RE.search(text)

    intent_det = {"intent": "ninguno", "method": None, "confidence": 0.0}
    if not (pago_match or confirm_match):
        # 2) Intento LLM clasificador SOLO si regex no detectó
        intent_det = await detectar_intencion_pago_confirmacion(text)

    # Resolver método si vino por regex:
    def _infer_method_from_text(t: str) -> Optional[str]:
        t = t.lower()
        if any(k in t for k in ["transferencia", "bancolombia", "davivienda"]):
            return "transferencia"
        if "payu" in t:
            return "payu"
        if any(k in t for k in ["pago en tienda", "efectivo", "contraentrega"]):
            return "pago_en_tienda"
        return None

    if pago_match or (intent_det["intent"] == "pago" and intent_det["confidence"] >= 0.6):
        method = intent_det["method"] or _infer_method_from_text(text) or "pago_en_tienda"
        actualizar_pedido_por_sesion(db, session_id, "metodo_pago", method)

        if method == "transferencia":
            return {
                "response": (
                    "Perfecto. Realiza la transferencia y envía el comprobante por este chat:\n"
                    "- Bancolombia: Cuenta Corriente No. 27480228756\n"
                    "- Davivienda: Cuenta Corriente No. 037169997501\n\n"
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
        else:  # pago_en_tienda
            return {
                "response": (
                    "Listo. Pagas directamente en la tienda al recoger tu pedido. "
                    "¿Te confirmo el pedido ya o quieres agregar otra prenda?"
                )
            }

    if confirm_match or (intent_det["intent"] == "confirmar" and intent_det["confidence"] >= 0.6):
        actualizar_pedido_por_sesion(db, session_id, "estado", "confirmado")
        pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
        try:
            enviar_pedido_a_hubspot(pedido_actualizado)
        except Exception:
            pass
        try:
            mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
            await enviar_mensaje_whatsapp("+573113305646", mensaje_alerta)
        except Exception:
            pass

        carrito = _carrito_load(pedido_actualizado)
        lineas = _cart_summary_lines(carrito)
        resumen = "\n".join(lineas)
        metodo_entrega = (pedido_actualizado.metodo_entrega or "").replace("_", " ")
        metodo_pago = (pedido_actualizado.metodo_pago or "").replace("_", " ")

        return {
            "response": (
                f"¡Pedido confirmado!\n\nResumen:\n{resumen}\n\n"
                f"Entrega: {metodo_entrega or 'pendiente'}\n"
                f"Pago: {metodo_pago or 'pendiente'}\n\n"
                "Te contactaremos en breve para coordinar el siguiente paso. "
                "¿Quieres agregar algo más?"
            )
        }



def _tiene_atributos_especificos(txt: str) -> bool:
    # Usa tu extractor de atributos + señales simples (talla/uso/manga/color)
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
    """
    Usa el LLM SOLO para clasificar intención y método de pago con un esquema cerrado.
    Devuelve: {"intent": "pago"|"confirmar"|"ninguno", "method": "transferencia"|"payu"|"pago_en_tienda"|null, "confidence": float}
    """
    try:
        schema_msg = (
            "Clasifica la intención del usuario respecto al flujo de compra.\n"
            "Responde SOLO JSON con estas claves:\n"
            "{\n"
            '  "intent": "pago" | "confirmar" | "ninguno",\n'
            '  "method": "transferencia" | "payu" | "pago_en_tienda" | null,\n'
            '  "confidence": number  // 0..1\n'
            "}\n\n"
            "Reglas:\n"
            "- Si el usuario expresa intención de pagar, intenta mapear método:\n"
            "  * transferencia, bancolombia, davivienda -> transferencia\n"
            "  * payu, link de pago en web -> payu\n"
            "  * efectivo, pago en tienda, contraentrega al recoger -> pago_en_tienda\n"
            "- Si el usuario quiere cerrar/confirmar/terminar el pedido -> intent = confirmar.\n"
            "- Si no aplica, pon intent='ninguno', method=null y confidence bajo (<=0.4).\n"
        )
        completion = client.chat.completions.create(
            model="gpt-4o-mini",  # más barato/rápido para clasificación
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": schema_msg},
                {"role": "user", "content": texto.strip()},
            ],
            max_tokens=350,
        )
        raw = completion.choices[0].message.content.strip()
        data = json.loads(raw)
        # Sanitiza por si acaso:
        intent = data.get("intent") if data.get("intent") in {"pago", "confirmar", "ninguno"} else "ninguno"
        method = data.get("method") if data.get("method") in {"transferencia","payu","pago_en_tienda"} else None
        conf = float(data.get("confidence") or 0.0)
        return {"intent": intent, "method": method, "confidence": conf}
    except Exception:
        return {"intent": "ninguno", "method": None, "confidence": 0.0}


# Catálogo breve para ofrecer categorías rápidamente
CATEGORIAS_RESUMEN = [
    "camisas (incluye guayaberas)", "jeans", "pantalones",
    "bermudas", "blazers", "suéteres", "camisetas",
    "calzado", "accesorios"
]
# Frases que el usuario usa para rechazar/refinar sugerencias
PATRONES_RECHAZO = [
    "esas no", "no me sirven", "no me gusta", "no me gustan", "no la quiero", "esa no es", "no es esa", "ninguna aplica",
    "otra", "otra opción", "otras", "otras opciones",
    "son manga corta", "quiero manga larga"
]

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(agent_router)

def _has_column(db: Session, table: str, col: str) -> bool:
    try:
        # SQLite: PRAGMA table_info
        rows = db.execute(text(f"PRAGMA table_info({table})")).fetchall()
        # Columna está en índice 1 del resultado de PRAGMA
        return any(r[1] == col for r in rows)
    except Exception:
        return False

def _ensure_column(col: str, ddl: str, table: str = "pedidos"):
    db = SessionLocal()
    try:
        if not _has_column(db, table, col):
            db.execute(text(ddl))
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


# (opcional) normaliza nulos de filas antiguas para que la lógica no falle
def _normalize_nulls():
    db = SessionLocal()
    try:
        db.execute(text("UPDATE pedidos SET saludo_enviado=0 WHERE saludo_enviado IS NULL"))
        db.execute(text("UPDATE pedidos SET datos_personales_advertidos=0 WHERE datos_personales_advertidos IS NULL"))
        db.execute(text("UPDATE pedidos SET sugeridos='' WHERE sugeridos IS NULL"))
        db.execute(text("UPDATE pedidos SET ultima_categoria='' WHERE ultima_categoria IS NULL"))
        db.execute(text("UPDATE pedidos SET ultimos_filtros='' WHERE ultimos_filtros IS NULL"))
        db.execute(text("UPDATE pedidos SET sugeridos_json='' WHERE sugeridos_json IS NULL"))
        db.execute(text("UPDATE pedidos SET ctx_json='' WHERE ctx_json IS NULL"))
        db.execute(text("UPDATE pedidos SET carrito_json='[]' WHERE carrito_json IS NULL"))
        db.execute(text("UPDATE pedidos SET preferencias_json='{}' WHERE preferencias_json IS NULL"))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

_normalize_nulls()

# ---------- Helpers de acceso directo ----------
def _get_saludo_enviado(db: Session, session_id: str) -> int:
    try:
        row = db.execute(text("SELECT saludo_enviado FROM pedidos WHERE session_id=:sid"), {"sid": session_id}).fetchone()
        return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0

def _get_last_msg_id(db: Session, session_id: str) -> Optional[str]:
    try:
        row = db.execute(text("SELECT last_msg_id FROM pedidos WHERE session_id=:sid"), {"sid": session_id}).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None

import json as _json

def _get_sugeridos_urls(db: Session, session_id: str) -> List[str]:
    """Devuelve la lista de URLs sugeridas (guardadas en la columna `sugeridos`)."""
    try:
        row = db.execute(
            text("SELECT sugeridos FROM pedidos WHERE session_id=:sid"),
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
    combined = list(dict.fromkeys(prev + nuevos))  # de-dupe preservando orden
    actualizar_pedido_por_sesion(db, session_id, "sugeridos", " ".join(combined))

def _set_sugeridos_list(db: Session, session_id: str, lista: List[dict]):
    """Guarda la lista completa de sugeridos (top N) como JSON en la fila del pedido."""
    try:
        db.execute(
            text("UPDATE pedidos SET sugeridos = :data WHERE session_id = :sid"),
            {"data": json.dumps(lista), "sid": session_id}
        )
        db.commit()
    except Exception:
        db.rollback()

# ✅ NUEVAS FUNCIONES PARA GUARDAR Y RECUPERAR FILTRO DEL USUARIO
def _set_user_filter(db: Session, session_id: str, filtro: dict):
    filtro_json = json.dumps(filtro)
    db.execute(text("UPDATE pedidos SET sugeridos = :filtro WHERE session_id = :sid"),
               {"filtro": filtro_json, "sid": session_id})
    db.commit()

def _get_user_filter(db: Session, session_id: str) -> Optional[dict]:
    row = db.execute(text("SELECT sugeridos FROM pedidos WHERE session_id = :sid"),
                     {"sid": session_id}).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None



def _set_sugeridos_list(db: Session, session_id: str, lista: List[dict]):
    """Guarda la lista completa de sugeridos (top N) como JSON en la fila del pedido."""
    try:
        db.execute(
            text("UPDATE pedidos SET sugeridos_json=:j WHERE session_id=:sid"),
            {"j": json.dumps(lista, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def _get_sugeridos_list(db: Session, session_id: str) -> List[dict]:
    """Recupera la lista JSON de sugeridos guardada anteriormente."""
    try:
        row = db.execute(
            text("SELECT sugeridos_json FROM pedidos WHERE session_id=:sid"),
            {"sid": session_id},
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else []
    except Exception:
        return []

def _get_ultima_cat_filters(db: Session, session_id: str):
    try:
        row = db.execute(
            text("SELECT ultima_categoria, ultimos_filtros FROM pedidos WHERE session_id=:sid"),
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

# ---------- Contexto de sesión (memoria por pedido) ----------
def _ctx_load(pedido) -> dict:
    try:
        sid = pedido.session_id
        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT ctx_json FROM pedidos WHERE session_id=:sid"),
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
            text("UPDATE pedidos SET ctx_json=:j WHERE session_id=:sid"),
            {"j": json.dumps(ctx, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def _remember_list(db: Session, session_id: str, cat: str, filtros: dict, productos: List[dict]):
    """Guarda: categoría/filtros + lista COMPLETA de sugeridos (no recortar claves)."""
    try:
        db.execute(
            text("UPDATE pedidos SET ultima_categoria=:c, ultimos_filtros=:f, sugeridos_json=:s WHERE session_id=:sid"),
            {
                "c": cat or "",
                "f": json.dumps(filtros, ensure_ascii=False),
                "s": json.dumps(productos, ensure_ascii=False),  # guarda objetos completos (incluye sku)
                "sid": session_id,
            },
        )
        db.commit()
    except Exception:
        db.rollback()

    # espejo en ctx (opcional)
    pedido = obtener_pedido_por_sesion(db, session_id)
    ctx = _ctx_load(pedido)
    ctx["ultima_categoria"] = cat
    ctx["ultimos_filtros"] = filtros
    ctx["ultima_lista"] = productos
    _ctx_save(db, session_id, ctx)

def _remember_selection(db: Session, session_id: str, prod: dict, idx: int):
    """Añade al historial la selección que hizo el usuario (opción N)."""
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
                text("SELECT carrito_json FROM pedidos WHERE session_id=:sid"),
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
            text("UPDATE pedidos SET carrito_json=:j WHERE session_id=:sid"),
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
                text("SELECT preferencias_json FROM pedidos WHERE session_id=:sid"),
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
            text("UPDATE pedidos SET preferencias_json=:j WHERE session_id=:sid"),
            {"j": json.dumps(prefs, ensure_ascii=False), "sid": session_id},
        )
        db.commit()
    except Exception:
        db.rollback()

def _cart_add(carrito: list, sku: str, nombre: str, categoria: str,
              talla: str = None, color: str = None, cantidad: int = 1,
              precio_unitario: float = 0.0):
    # merge por sku+talla+color
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
        precio = f"${int(it.get('precio_unitario', 0)):,.0f}"
        qty = int(it.get("cantidad", 1))
        tail = " ".join([x for x in [(it.get("color") or ""), (it.get("talla") or "")] if x]).strip()
        tail = f" {tail}" if tail else ""
        lines.append(f"{i}. {it['nombre']} ({it['sku']}){tail} x{qty} – {precio} c/u")
    lines.append(f"\nTotal: ${int(_cart_total(carrito)):,.0f}")
    return lines


#  Prompt maestro
with open("prompt_cassany_gpt_final.txt", "r", encoding="utf-8") as fh:
    base_prompt = fh.read().strip()

# 👇 NUEVO: protocolo para obligar acciones de carrito en JSON
ACTIONS_PROTOCOL = """
=== PROTOCOLO DE ACCIONES (OBLIGATORIO) ===
Cuando el usuario pida operar el carrito (agregar, quitar, ver, cambiar talla), RESPONDE SOLO con JSON válido (sin texto extra) usando exactamente uno de:
{"action":"ADD_TO_CART","product_ref":"<n|id|url>","size":null}
{"action":"REMOVE_FROM_CART","product_id":123}
{"action":"SHOW_CART"}
{"action":"ASK_VARIANT","missing":"size"}
{"action":"CLARIFY","question":"¿Cuál talla prefieres?"}
- "product_ref": acepta el índice mostrado al usuario (1,2,3), el id del producto o su URL.
- Si el producto requiere talla y el usuario no la dio, usa ASK_VARIANT.
- Si el usuario dice “agrega el 1”, usa {"action":"ADD_TO_CART","product_ref":"1"}.
- NUNCA mezcles texto humano con el JSON; la respuesta debe ser solo el JSON.
""".strip()

#  Pydantic models
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

#  Dependencia DB
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

#  Utilidad: expirar pedidos inactivos
def depurar_pedidos_expirados(db: Session):
    from models import Pedido
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


#  Helper LLM
ALLOWED_CAMPOS = {
    "producto", "talla", "cantidad", "metodo_entrega", "direccion",
    "punto_venta", "metodo_pago", "estado", "nombre_cliente", "telefono",
    "email", "ciudad", "precio_unitario", "subtotal"
}

def _clean_json(texto: str) -> dict:
    """Intenta extraer el primer {...} válido que encuentre."""
    try:
        start, end = texto.find("{"), texto.rfind("}")
        if start == -1 or end == -1:
            raise ValueError
        return json.loads(texto[start:end + 1])
    except Exception:
        # Fallback amable (evita el mensaje genérico)
        return {
            "campos": {},
            "respuesta": "Puedo continuar con tu compra. ¿Quieres que agregue el producto que te gustó al carrito o prefieres ver el carrito primero?",
        }

async def procesar_conversacion_llm(pedido, texto_usuario: str):
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

    # ---------- Sugerencias de productos (máx. 3) ----------
    productos = []
    mensaje = None

    # 1) Intento principal: usar el texto tal cual (incluye atributos como "blancas", "manga larga")
    sug = sugerir_productos(texto_usuario, limite=3)
    if isinstance(sug, dict):
        productos = (sug.get("productos") or [])[:3]
        mensaje = sug.get("mensaje")

    # 2) Plan B: si no hubo resultados, intenta por categoría detectada (ej. "camisas", "jeans")
    if not productos:
        cat, _ = detectar_categoria(texto_usuario)
        if cat:
            sug2 = sugerir_productos(cat, limite=3)
            if isinstance(sug2, dict):
                productos = (sug2.get("productos") or [])[:3]
                # si el primer intento no trajo mensaje y este sí, úsalo
                if not mensaje:
                    mensaje = sug2.get("mensaje")

    # 3) Entregar al LLM
    if productos:
        extras["productos_disponibles"] = productos  # estructura: [{sku?, nombre, url, precio, tallas_disponibles?}, ...]
    elif mensaje:
        extras["mensaje_sugerencias"] = mensaje      # p.ej. "No hay stock en esta categoría"


    # ---------- Puntos de venta si aplica ----------
    if estado["metodo_entrega"] == "recoger_en_tienda" and not estado["punto_venta"]:
        extras["puntos_venta"] = PUNTOS_VENTA

    # ---------- Instrucciones al LLM (LLM-first + ACCIONES) ----------
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

        "Si en Contexto_extra hay 'productos_disponibles', preséntalos en una lista numerada (1..n) con el formato "
        "'1. Nombre - $precio - URL'. Máximo 3 ítems y luego haz una pregunta útil (talla/cantidad). "
        "Cuando muestres esa lista, incluye además la acción 'cache_list' con los mismos elementos listados para que el sistema los recuerde.\n\n"

        "Si hay 'mensaje_sugerencias' (p. ej. sin stock), comunícalo brevemente y ofrece buscar algo similar o explorar otra categoría; nunca inventes disponibilidad.\n\n"

        "Varía los conectores iniciales ('Claro', 'Entendido', 'De acuerdo', etc.). "
        "Si el cliente ya tiene tallas preferidas, puedes proponerlas por defecto en 'add_item'.\n\n"

        "Antes de pedir datos personales para envíos, recuerda informar la política: "
        "https://cassany.co/tratamiento-de-datos-personales/ (solo una vez por sesión)."
    )

    # ---------- Contexto enriquecido: Carrito + Perfil ----------
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
                {"role": "system", "content": ACTIONS_PROTOCOL},   # 👈 NUEVO
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,           # un poco más bajo para respuestas más consistentes
            max_tokens=1000,            # un pelín más de aire para lista + acciones
            response_format={"type": "json_object"}  # 👈 JSON estricto
        )
        raw = completion.choices[0].message.content.strip()
        print("[DBG] respuesta LLM (raw):", raw)
        data = json.loads(raw) 
        print("[DBG] json parsed:", data)
        return data          # parseo directo
    except Exception as e:
        import traceback
        print("[LLM_ERR]", repr(e))
        traceback.print_exc()
        print("RAW LLM:", raw if 'raw' in locals() else "")

        data = _clean_json(raw if 'raw' in locals() else "{}")

        # filtrar solo los campos permitidos
        raw_campos = data.get("campos", {})
        if not isinstance(raw_campos, dict):
            raw_campos = {}
        _ = {k: v for k, v in raw_campos.items() if k in ALLOWED_CAMPOS}

        return data

# Habilitar CORS para el webchat
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],               # opcional: cambia "*" por ["https://innobytedevelop.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _formatear_sugerencias(lista: List[dict]) -> str:
    lines = []
    for i, p in enumerate(lista[:3], start=1):
        precio = f"${p['precio']:,}"
        lines.append(f"{i}. {p['nombre']} - {precio} - {p['url']}")
    return "Aquí tienes algunas opciones:\n" + "\n".join(lines)

#  Endpoint conversación
@app.post("/mensaje-whatsapp")
async def mensaje_whatsapp(user_input: UserMessage, session_id: str, db: Session = Depends(get_db)):
    depurar_pedidos_expirados(db)

    ahora = datetime.now(timezone.utc)
    pedido = obtener_pedido_por_sesion(db, session_id)

    # Si no existe, créalo inmediatamente (evita AttributeError)
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
            "sugeridos_json": "",
            # nuevas
            "carrito_json": "[]",
            "preferencias_json": "{}",
        })
        pedido = obtener_pedido_por_sesion(db, session_id)

    # A partir de aquí, pedido SIEMPRE existe
    last_act_utc = _to_utc(getattr(pedido, "last_activity", None))
    tiempo_inactivo = ahora - last_act_utc

    text = user_input.message.strip().lower()
    actualizar_pedido_por_sesion(db, session_id, "last_activity", ahora)

    filtros_detectados = {}

    m_color = COLOR_RE.search(text)
    if m_color:
        filtros_detectados["color"] = m_color.group(1).lower()

    m_talla = TALLA_RE.search(text)
    if m_talla:
        filtros_detectados["talla"] = m_talla.group(1).upper() if m_talla.lastindex else m_talla.group(0).upper()

    m_manga = MANGA_RE.search(text)
    if m_manga:
        filtros_detectados["manga"] = m_manga.group(1).lower()

    m_uso = USO_RE.search(text)
    if m_uso:
        filtros_detectados["uso"] = m_uso.group(1).lower()

    if filtros_detectados:
        print("[DBG] Guardando filtros:", filtros_detectados)
        _set_user_filter(db, session_id, filtros_detectados)


    # Si estuvo inactivo > TIMEOUT_MIN, reinicia limpio (y avisa)
    if tiempo_inactivo.total_seconds() / 60 > TIMEOUT_MIN:
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
            # nuevas
            "carrito_json": "[]",
            "preferencias_json": "{}",
        })
        return {
            "response": "¡Hola de nuevo! Pasó un buen rato sin actividad, así que reinicié la conversación. ¿Qué te gustaría ver hoy?"
        }

    # Vincula teléfono desde el session_id si falta
    if not getattr(pedido, "telefono", None) and session_id.startswith("cliente_"):
        telefono_cliente = session_id.replace("cliente_", "")
        actualizar_pedido_por_sesion(db, session_id, "telefono", telefono_cliente)

    # --- Manejo de sesión cancelada ---
    if pedido and pedido.estado == "cancelado":
        if re.match(r'^(hola|buen(?:o|a)s? días?|buenas tardes|buenas noches|hey)\b', text):
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
                "carrito_json": "[]",
                "preferencias_json": "{}",
            })
            return {
                "response": (
                    "Bienvenido a CASSANY. Estoy aquí para ayudarte con tu compra.\n"
                    "Si prefieres, también puedes comunicarte directamente con la tienda de tu preferencia:\n\n"
                    "C.C Fabricato – 3103380995\n"
                    "C.C Florida – 3207335493\n"
                    "Centro - Junín – 3207339281\n"
                    "C.C La Central – 3207338021\n"
                    "C.C Mayorca – 3207332984\n"
                    "C.C Premium Plaza – 3207330457\n"
                    "C.C Unicentro – 3103408952"
                )
            }
        return {
            "response": (
                "No tienes ningún pedido activo en este momento. "
                "Escribe ‘hola’ cuando quieras comenzar una nueva compra."
            )
        }

    # 👉 Atención personalizada
    if detectar_intencion_atencion(text):
        mensaje_alerta = generar_mensaje_atencion_humana(pedido)
        await enviar_mensaje_whatsapp("+573113305646", mensaje_alerta)
        return {
            "response": (
                "Entendido, ya te pongo en contacto con uno de nuestros asesores. "
                "Te responderán personalmente en breve para ayudarte con lo que necesitas."
            )
        }

    # 👉 Cancelación parcial o total
    if any(neg in text for neg in ["ya no quiero", "cancelar pedido", "no deseo", "me arrepentí"]):
        producto_cancelado = pedido.producto or "el pedido actual"
        actualizar_pedido_por_sesion(db, session_id, "estado", "cancelado")
        actualizar_pedido_por_sesion(db, session_id, "producto", "")
        actualizar_pedido_por_sesion(db, session_id, "talla", "")
        actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "")
        actualizar_pedido_por_sesion(db, session_id, "punto_venta", "")
        return {
            "response": f"Entiendo, he cancelado {producto_cancelado}. ¿Te gustaría ver otra prenda o necesitas ayuda con algo más?"
        }

    # 👉 Saludo inicial (una sola vez por sesión)
    if SALUDO_RE.match(text):
        if _get_saludo_enviado(db, session_id) == 0:
            actualizar_pedido_por_sesion(db, session_id, "saludo_enviado", 1)
            return {
                "response": (
                    "Bienvenido a CASSANY. Estoy aquí para ayudarte con tu compra.\n"
                    "Si prefieres, también puedes comunicarte directamente con la tienda de tu preferencia por WhatsApp.\n\n"
                    "C.C Fabricato – 3103380995\n"
                    "C.C Florida – 3207335493\n"
                    "Centro - Junín – 3207339281\n"
                    "C.C La Central – 3207338021\n"
                    "C.C Mayorca – 3207332984\n"
                    "C.C Premium Plaza – 3207330457\n"
                    "C.C Unicentro – 3103408952"
                )
            }
        return {"response": "¡Hola! ¿Qué te gustaría ver hoy: camisas, jeans, pantalones o suéteres?"}
    
    # 👉 Atajos de intención explícita (antes del LLM)
    if DOMICILIO_RE.search(text):
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

    if RECOGER_RE.search(text):
        actualizar_pedido_por_sesion(db, session_id, "metodo_entrega", "recoger_en_tienda")
        tiendas = "\n".join(PUNTOS_VENTA)
        return {"response": f"Por favor, confirma en cuál de nuestras tiendas deseas recoger tu pedido:\n{tiendas}"}
    
    # 👉 Small-talk
    if SMALLTALK_RE.search(text):
        cats = ", ".join(CATEGORIAS_RESUMEN[:4]) + "…"
        return {
            "response": (
                f"¡Con gusto! ¿Te muestro algo hoy? Tenemos {cats} "
                "¿Qué prefieres ver primero?"
            )
        }

    # 👉 Preguntas generales / off-topic
    if OFFTOPIC_RE.search(text):
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {
            "response": (
                "Somos CASSANY, una marca de ropa para hombre. "
                "Trabajamos estas categorías:\n"
                f"{cats}\n\n"
                "¿Te muestro camisas o prefieres otra categoría?"
            )
        }

    # 👉 Descubrimiento/indecisión
    if DISCOVERY_RE.search(text):
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {
            "response": (
                "¡Te ayudo a elegir! Dime por favor:\n"
                "1) ¿Qué te interesa ver primero?\n"
                f"{cats}\n"
                "2) ¿Cuál es tu talla? (por ejemplo: S, M, L, XL)\n"
                "3) ¿Tienes ocasión o estilo en mente? (oficina, casual, evento)\n"
                "Con eso te muestro opciones acertadas."
            )
        }
    
        # 👉 Ver carrito (determinista)
    if CARRO_RE.search(text):
        carrito = _carrito_load(pedido)
        lineas = _cart_summary_lines(carrito)
        return {"response": "\n".join(lineas)}

    # 👉 "Fotos de <categoría>" → prioriza LLM si hay atributos; si no, handler rápido
    m_fotos = FOTOS_RE.search(text)
    if m_fotos:
        # Si el usuario especifica atributos (manga, color, talla, uso), NO listamos aquí:
        # dejamos que el LLM procese para respetar filtros y devolver acciones (cache_list, add_item, etc.)
        if _tiene_atributos_especificos(text):
            pass  # no retornes; continúa el flujo para que llegue a procesar_conversacion_llm(...)
        else:
            # Handler rápido SOLO si no hay atributos finos
            cat_txt = m_fotos.group(2).strip()
            cat, _ = detectar_categoria(cat_txt)  # si no detecta, usamos el texto tal cual
            consulta = cat or cat_txt

            urls_previas = _get_sugeridos_urls(db, session_id)
            res = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)  # <= 12 aquí
            productos = res.get("productos", [])

            if productos:
                # Persistimos categoría/filtros/lista COMPLETA (para paginar)
                filtros = detectar_atributos(cat_txt) or {}
                _remember_list(db, session_id, cat or "", filtros, productos)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if p.get("url")])

                # Mostramos SOLO 3 al usuario
                return {"response": _formatear_sugerencias(productos[:3])}

            # Sin resultados → mensaje claro + alternativas
            cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
            return {"response": f"No hay stock para «{cat_txt}» en este momento. ¿Te muestro algo de:\n{cats}"}


    # 👉 “Muéstrame / puedes mostrarme / quiero ver …”
    if MOSTRAR_RE.search(text):
        # ⛔️ Si el usuario especifica atributos (color, manga, talla, uso), NO respondas aquí.
        # Deja que el LLM procese para respetar los filtros y devolver acciones (add_item, cache_list, etc.)
        if _tiene_atributos_especificos(text):
            pass  # no retornes; continúa el flujo para que llegue a procesar_conversacion_llm(...)
        else:
            # Handler rápido sólo cuando NO hay atributos finos
            ultima_cat, _ = _get_ultima_cat_filters(db, session_id)
            cat, _ = detectar_categoria(text)
            consulta = cat or ultima_cat
            if consulta:
                urls_previas = _get_sugeridos_urls(db, session_id)
                res = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)  # <= 12 aquí
                productos = res.get("productos", [])
                if productos:
                    # Guardamos la LISTA COMPLETA para paginar
                    _set_sugeridos_list(db, session_id, productos)
                    _append_sugeridos_urls(db, session_id, [p["url"] for p in productos])

                    # Mostramos SOLO 3
                    return {"response": _formatear_sugerencias(productos[:3])}
            cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
            return {"response": f"¿Qué te muestro primero?\n{cats}"}


    # 👉 "Más opciones" con persistencia de lista (para “opción N”)
    if MAS_OPCIONES_RE.search(text):
        # Primero intenta continuar desde la lista previa
        productos_previos = _get_sugeridos_list(db, session_id)
        if productos_previos:
            restantes = productos_previos[3:] if len(productos_previos) > 3 else []
            if restantes:
                _set_sugeridos_list(db, session_id, restantes)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in restantes])
                return {"response": _formatear_sugerencias(restantes)}
            else:
                return {"response": "Ya te mostré todas las opciones disponibles por ahora. ¿Quieres buscar algo diferente?"}

        # Si no hay lista previa, usamos el flujo anterior para buscar más resultados filtrados
        ultima_cat, ult_filtros = _get_ultima_cat_filters(db, session_id)

        if not ultima_cat:
            ultima_cat, _ = detectar_categoria(text)

        if ultima_cat:
            partes = [ultima_cat]
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
                filtros_persist = ult_filtros if isinstance(ult_filtros, dict) and ult_filtros else detectar_atributos(text)
                _remember_list(db, session_id, ultima_cat, filtros_persist, productos)
                _append_sugeridos_urls(db, session_id, [p["url"] for p in productos])
                return {"response": _formatear_sugerencias(productos)}
            else:
                msg = (res.get("mensaje") if isinstance(res, dict) else None) or \
                    f"No hay stock en la categoría «{ultima_cat}» en este momento."
                return {"response": msg + " ¿Te muestro algo similar?"}

        # Si no hay categoría reconocida
        cats = "\n- " + "\n- ".join(CATEGORIAS_RESUMEN)
        return {"response": f"No detecté ninguna categoría concreta en tu solicitud. ¿Te gustaría que te muestre opciones de:\n{cats}"}



    # 👉 Selección de una opción mostrada (por número 1..n)
    m_sel = SELECCION_RE.search(text)
    if m_sel:
        num_txt = next((g for g in m_sel.groups() if g), None)
        if num_txt:
            idx = int(num_txt) - 1
            lista = _get_sugeridos_list(db, session_id)  # última lista persistida
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

                tallas = prod.get("tallas_disponibles") or []
                # 👇 NUEVO: si el usuario pidió "agrega...", agregamos directo (pidiendo talla si hace falta)
                if ADD_RE.search(text):
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
                return {"response": f"Listo, seleccionaste la opción {idx+1}. ¿Cuántas unidades deseas?"}
            if lista:
                return {"response": f"Por favor indícame un número entre 1 y {len(lista)} de la lista que te mostré."}

    # 👉 Rechazo / refinamiento (no repetir, forzar nuevas opciones) — con persistencia de lista
    if any(pat in text for pat in PATRONES_RECHAZO):
        urls_previas = _get_sugeridos_urls(db, session_id)
        res = sugerir_productos(text, limite=3, excluir_urls=urls_previas)
        productos = res.get("productos", [])

        if len(productos) < 3:
            cat_relajada, _ = detectar_categoria(text)
            if cat_relajada:
                res2 = sugerir_productos(cat_relajada, limite=3, excluir_urls=urls_previas)
                ya = {p["url"] for p in productos}
                productos += [p for p in res2.get("productos", []) if p["url"] not in ya]

        if productos:
            try:
                cat_local, _ = detectar_categoria(text)
                filtros = detectar_atributos(text)  # {'manga':..., 'subtipo':..., 'color':...}
                actualizar_pedido_por_sesion(db, session_id, "ultima_categoria", cat_local or "")
                actualizar_pedido_por_sesion(db, session_id, "ultimos_filtros", json.dumps(filtros, ensure_ascii=False))
                _set_sugeridos_list(db, session_id, productos)  # guarda top-N para “opción N”
            except Exception:
                pass

            _append_sugeridos_urls(db, session_id, [p["url"] for p in productos])
            return {"response": _formatear_sugerencias(productos)}
        else:
            msg = res.get("mensaje") or "No encontré opciones que cumplan lo que pides. ¿Te muestro algo similar?"
            return {"response": msg}
        

    # 👉 Selección directa por índice: "opción 1", "la 2", "3", etc.
    m = SELECCION_RE.search(text)
    if m:
        # toma el primer grupo no vacío
        idx_str = next((g for g in m.groups() if g), None)
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            idx = None

        if idx is not None:
            lista = _get_sugeridos_list(db, session_id)  # lo que listaste antes
            if lista and 1 <= idx <= len(lista):
                prod = lista[idx - 1]
                # Guarda la selección en el historial (para luego asociar talla/cantidad)
                _remember_selection(db, session_id, prod, idx)
                # Opcional: deja marcado el producto en el pedido (nombre como referencia)
                actualizar_pedido_por_sesion(db, session_id, "producto", prod.get("nombre", ""))

                return {
                    "response": (
                        f"Anotado: opción {idx} — {prod.get('nombre', 'Producto')}.\n"
                        "¿Qué talla necesitas y cuántas unidades?"
                    )
                }
            else:
                return {"response": "No tengo esa opción disponible. ¿Te muestro nuevas alternativas?"}


    # 👉 Procesar mensaje con LLM (LLM-first para “¿cuáles jeans/camisas tienes?”)
    resultado = await procesar_conversacion_llm(pedido, text)

    if not isinstance(resultado, dict):
        return {"response": "Disculpa, ocurrió un error procesando tu solicitud. ¿Te muestro opciones de camisas o jeans?"}
    
    # 🩹 Fallback por si el LLM no devuelve acción 'cache_list'
    acciones_llm = resultado.get("acciones", [])
    if not any((a.get("tipo") == "cache_list") for a in acciones_llm):
        productos_previos = _get_sugeridos_list(db, session_id)
        if not productos_previos:
            # Detectamos categoría y atributos por si acaso
            categoria_detectada, _ = detectar_categoria(text)
            filtros = detectar_atributos(text)
            partes = [categoria_detectada] if categoria_detectada else []

            if filtros.get("subtipo") == "guayabera":
                partes.append("guayabera")
            if filtros.get("manga") in ("corta", "larga"):
                partes.append(f"manga {filtros['manga']}")
            if filtros.get("color"):
                partes.append(filtros["color"])
            if filtros.get("talla"):
                partes.append(f"talla {filtros['talla']}")
            if filtros.get("uso"):
                partes.append(filtros["uso"])

            consulta = " ".join(partes).strip()
            urls_previas = _get_sugeridos_urls(db, session_id)
            res_fallback = sugerir_productos(consulta or text, limite=12, excluir_urls=urls_previas)  # <= 12 aquí
            productos = res_fallback.get("productos", [])

            if productos:
                print("[DBG] Fallback: forzando guardado de productos (no hubo cache_list)")
                _remember_list(db, session_id, categoria_detectada or "", filtros or {}, productos)  # guarda TODO
                _append_sugeridos_urls(db, session_id, [p["url"] for p in productos if "url" in p])

                # Si el LLM no envió cache_list pero el fallback encontró productos,
                # sobrescribimos la respuesta "no hay stock" con una lista corta útil.
                resultado["respuesta"] = _formatear_sugerencias(productos[:3]) + \
                    "\n¿Te muestro más opciones o agrego alguna al carrito?"

    # 👉 Ejecutar acciones solicitadas por el LLM (carrito/memoria/checkout/cache_list)
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
                    # Solo formateamos luego; no cambia estado
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
                    # Gancho para bloqueo de pedido / notificaciones
                    pass
                elif t == "cache_list":
                    productos = args.get("productos") or []
                    if isinstance(productos, list) and productos:
                        try:
                            # 1) Guarda lo que envió el LLM
                            _set_sugeridos_list(db, session_id, productos)
                            _append_sugeridos_urls(
                                db, session_id,
                                [p.get("url") for p in productos if isinstance(p, dict) and p.get("url")]
                            )

                            # 2) 🔼 Top-up: si vinieron pocas (1–3), intenta ampliar hasta ~12 coherentes
                            if len(productos) <= 3:
                                # Recupera categoría/filtros persistidos; si no hay, detecta desde el texto actual
                                ultima_cat, ult_filtros = _get_ultima_cat_filters(db, session_id)
                                if not ultima_cat:
                                    ultima_cat, _ = detectar_categoria(text)

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

                                consulta = " ".join([p for p in partes if p]).strip() or (text or "")
                                urls_previas = _get_sugeridos_urls(db, session_id)

                                res_plus = sugerir_productos(consulta, limite=12, excluir_urls=urls_previas)
                                extra = res_plus.get("productos", [])

                                if extra:
                                    # merge sin duplicar por URL
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

        # Mantén sincronizado el subtotal
        try:
            actualizar_pedido_por_sesion(db, session_id, "subtotal", _cart_total(carrito))
        except Exception:
            pass

        # Si alguna acción pidió mostrar el carrito, anteponer el resumen
        if any((a.get("tipo") == "show_cart") for a in acciones):
            lineas = _cart_summary_lines(carrito)
            resultado["respuesta"] = ( "\n".join(lineas) + "\n\n" + (resultado.get("respuesta") or "") ).strip()

    # 👉 Guardar campos devueltos por el modelo
    campos_dict = resultado.get("campos", {})
    if isinstance(campos_dict, dict):
        if "producto" in campos_dict:
            actualizar_pedido_por_sesion(db, session_id, "talla", "")
            actualizar_pedido_por_sesion(db, session_id, "cantidad", 0)
        for campo, val in campos_dict.items():
            if campo in ALLOWED_CAMPOS:
                actualizar_pedido_por_sesion(db, session_id, campo, val)
        if ("talla" in campos_dict) or ("cantidad" in campos_dict):
            _update_last_selection_from_pedido(db, session_id)

    # 👉 Confirmar punto de venta si elige recoger en tienda
    if campos_dict.get("metodo_entrega") == "recoger_en_tienda" and not campos_dict.get("punto_venta"):
        tiendas = "\n".join(PUNTOS_VENTA)
        return {"response": f"Por favor, confirma en cuál de nuestras tiendas deseas recoger tu pedido:\n{tiendas}"}

    # 👉 Entrega a domicilio: política una sola vez y luego pedir dirección
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

    # 👉 Una vez que ya tenemos dirección y ciudad, pedir método de pago
    if campos_dict.get("direccion") and campos_dict.get("ciudad"):
        return {
            "response": (
                "Perfecto, he registrado tu dirección y ciudad.\n\n"
                "Por favor, confirma el método de pago que prefieres:\n"
                "- Transferencia a Bancolombia: Cuenta Corriente No. 27480228756\n"
                "- Transferencia a Davivienda: Cuenta Corriente No. 037169997501\n"
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

    # 👉 Enviar notificación a HubSpot y WhatsApp al confirmar
    if campos_dict.get("estado") == "confirmado":
        pedido_actualizado = obtener_pedido_por_sesion(db, session_id)
        enviar_pedido_a_hubspot(pedido_actualizado)
        mensaje_alerta = generar_mensaje_atencion_humana(pedido_actualizado)
        await enviar_mensaje_whatsapp("+573113305646", mensaje_alerta)

    return {"response": resultado.get("respuesta", "Disculpa, ocurrió un error.")}

# ------------------ Verificación del webhook (GET) ------------------
@app.get("/webhook")
def root():
    return {"ok": True, "service": "cassany", "build": APP_BUILD, "docs": "/docs"}

def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(400, "Token de verificación inválido.")

#  Webhook Meta y envío
@app.post("/webhook")
async def receive_whatsapp_message(request: Request):
    data = await request.json()
    print("📥 MENSAJE RECIBIDO DE WHATSAPP:\n", data)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {}) or {}

            # Ignora entregas/lecturas/reacciones
            if value.get("statuses"):
                continue

            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue

                num = msg.get("from")
                txt = (msg.get("text") or {}).get("body", "")
                msg_id = msg.get("id")  # wamid

                if not (num and txt and msg_id):
                    continue

                session_id = f"cliente_{num}"
                db = SessionLocal()

                try:
                    # DEDUPE por wamid
                    if _get_last_msg_id(db, session_id) == msg_id:
                        continue

                    print(f"🧪 Texto recibido: {txt}")

                    res = await mensaje_whatsapp(UserMessage(message=txt), session_id=session_id, db=db)
                    # Marca el último mensaje procesado
                    actualizar_pedido_por_sesion(db, session_id, "last_msg_id", msg_id)

                    await enviar_mensaje_whatsapp(num, res["response"])
                finally:
                    db.close()
    return {"status": "received"}


async def enviar_mensaje_whatsapp(numero: str, mensaje: str):
    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_NUMBER}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": mensaje}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10).raise_for_status()
        print("✅ Mensaje enviado a WhatsApp")
    except Exception as exc:
        print("❌ Error envío WhatsApp:", exc)
        print("🚀 Enviando mensaje a:", numero)
        print("📨 Contenido:", mensaje)
        print("🚀 Llamando a enviar_mensaje_whatsapp")
        print(f"📨 A: {numero} | Mensaje: {mensaje}")

@app.get("/__version")
def version():
    return {"build": APP_BUILD}

@app.get("/test-whatsapp")
async def test_whatsapp():
    await enviar_mensaje_whatsapp("+573113305646", "🚀 Token nuevo activo. Esta es una prueba en vivo.")
    return {"status": "sent"}


init_db()

