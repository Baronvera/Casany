# utils_intencion.py — v2.0
import re
import unicodedata
from typing import List

def _norm(s: str) -> str:
    """Minúsculas y sin tildes/diacríticos."""
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

# Palabras/frases que fuertemente indican querer hablar con alguien
# (usamos regex con límites de palabra para evitar falsos positivos)
PATRONES_POSITIVOS: List[re.Pattern] = [
    # hablar/escribir/comunicar con alguien/persona/asesor/humano
    re.compile(r"\b(hablar|escribir|chatear|comunicar(?:me)?)\s+con\s+(alguien|una\s+persona|un\s+asesor|asesor|humano)\b"),
    # poner/pasar/conectar con asesor/humano/persona
    re.compile(r"\b(pon(?:me|er)|p[aá]same|con[eé]ct(?:a|ame)|conectar(?:me)?)\s+con\s+(un|una|el|la)?\s*(asesor|humano|persona)\b"),
    # quiero/puedo hablar con asesor/persona/humano
    re.compile(r"\b(quiero|puedo|necesito)\s+(hablar|comunicarme)\s+con\s+(un|una|el|la)?\s*(asesor|persona|humano)\b"),
    # atencion/trato humana/personalizada
    re.compile(r"\b(atenci[oó]n|trato)\s+(humana|personalizada|directa)\b"),
    # siempre hablo con / me atiende <nombre>
    re.compile(r"\b(siempre\s+hablo\s+con|me\s+atiende)\s+\w+\b"),
    # envíame/mándame fotos
    re.compile(r"\b(env[ií]ame|m[aá]ndame)\s+fotos\b"),
    # muéstrame/quiero ver lo que queda / las fotos
    re.compile(r"\b(mu[eé]strame|quiero\s+ver)\s+(lo\s+que\s+queda|las\s+fotos|el\s+cat[aá]logo\s+real)\b"),
    # frases sueltas frecuentes
    re.compile(r"\b(asesor|humano)\b"),
]

# Filtros negativos: si hay negación cerca de las palabras clave, NO activar
PATRONES_NEGATIVOS: List[re.Pattern] = [
    # no + (hablar|asesor|persona|humano|atencion)
    re.compile(r"\bno\b.*\b(hablar|asesor|persona|humano|atenci[oó]n)\b"),
    # prefiero seguir aqui / con el bot / por chat
    re.compile(r"\b(prefiero|quiero)\s+(seguir|continuar)\s+(aqu[ií]|por\s+chat|con\s+el\s+bot)\b"),
    # no necesito / no requiero asesor / atencion
    re.compile(r"\b(no\s+(necesito|requiero))\s+(asesor|atenci[oó]n|persona|humano)\b"),
]

def detectar_intencion_atencion(texto: str) -> bool:
    """
    Devuelve True si el usuario probablemente quiere atención humana.
    Aplica normalización, patrones positivos y filtros negativos.
    """
    t = _norm(texto)

    # Si se detecta negación clara, no activar
    for pat in PATRONES_NEGATIVOS:
        if pat.search(t):
            return False

    # Positivos: basta con que uno encaje
    for pat in PATRONES_POSITIVOS:
        if pat.search(t):
            return True

    return False


