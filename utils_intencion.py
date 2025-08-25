# utils_intencion.py — v2.2 (corregido y optimizado)
import re
import unicodedata
from typing import List

def _norm(s: str) -> str:
    """Normaliza el texto a minúsculas y sin tildes/diacríticos."""
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

# Patrones POSITIVOS: expresiones que indican intención clara de hablar con un humano/asesor
PATRONES_POSITIVOS: List[re.Pattern] = [
    # hablar/escribir/chatear/comunicar con alguien/persona/asesor/humano
    re.compile(r"\b(hablar|escribir|chatear|comunicar(?:me)?)\s+con\s+(alguien|persona|asesor|humano)\b"),
    # poner/pasar/conectar con asesor/humano/persona
    re.compile(r"\b(pon(?:me|er)|pasame|conect(?:a|ame)|conectar(?:me)?)\s+con\s+(asesor|humano|persona)\b"),
    # quiero/puedo/necesito/prefiero hablar con asesor/persona/humano
    re.compile(r"\b(quiero|puedo|necesito|prefiero)\s+(hablar|comunicarme|atencion)\s+con\s+(asesor|persona|humano)\b"),
    # atencion/trato humano/personalizado/directo
    re.compile(r"\b(atencion|trato)\s+(humana|personalizada|directa|real)\b"),
    # expresiones de urgencia: "quiero un asesor humano ya"
    re.compile(r"\b(quiero|necesito).*(asesor|humano|persona real)\b"),
    # siempre hablo con / me atiende <nombre>
    re.compile(r"\b(siempre\s+hablo\s+con|me\s+atiende)\s+\w+\b"),
    # envíame/mándame fotos reales/catálogo
    re.compile(r"\b(env(iame|iame)|mandame)\s+(fotos|imagenes|catalogo(\s+real)?)\b"),
    # muéstrame/quiero ver fotos reales/catálogo
    re.compile(r"\b(muestrame|quiero\s+ver)\s+(fotos|imagenes|catalogo(\s+real)?)\b"),
    # pedir numero o contacto de asesor
    re.compile(r"\b(numero|contacto)\s+(de\s+)?(asesor|humano|persona)\b"),
]

# Patrones NEGATIVOS: evitan falsos positivos si hay negación clara
PATRONES_NEGATIVOS: List[re.Pattern] = [
    # negaciones explícitas: no quiero hablar con asesor/humano/persona/atencion
    re.compile(r"\bno\b.*\b(hablar|asesor|persona|humano|atencion)\b"),
    # prefiero seguir aqui / con el bot / por chat
    re.compile(r"\b(prefiero|quiero)\s+(seguir|continuar)\s+(aqui|por\s+chat|con\s+el\s+bot)\b"),
    # no necesito / no requiero asesor / atencion
    re.compile(r"\b(no\s+(necesito|requiero))\s+(asesor|atencion|persona|humano)\b"),
]

def detectar_intencion_atencion(texto: str) -> bool:
    """
    Devuelve True si el usuario probablemente quiere atención humana.
    Aplica normalización, patrones positivos y filtros negativos.
    """
    t = _norm(texto)

    # Primero revisamos patrones negativos (niegan explícitamente la intención)
    for pat in PATRONES_NEGATIVOS:
        if pat.search(t):
            return False

    # Luego patrones positivos (basta con que uno encaje)
    for pat in PATRONES_POSITIVOS:
        if pat.search(t):
            return True

    return False
