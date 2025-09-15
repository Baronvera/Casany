# filtros.py
import re
import unicodedata
from typing import Optional

# Tus regex tal cual:
SALUDO_RE  = re.compile(r'^\s*(hola|buenas(?:\s+(tardes|noches))?|buen(?:o|a)s?\s*d[ií]as?|hey)\b', re.I)
MAS_OPCIONES_RE = re.compile(r'\b(más opciones|mas opciones|muéstrame más|muestrame mas|ver más|ver mas)\b', re.I)
DOMICILIO_RE = re.compile(r'\b(a\s*domicilio|env[ií]o\s*a\s*domicilio|domicilio)\b', re.I)
RECOGER_RE  = re.compile(r'\b(recoger(?:lo)?\s+en\s+(tienda|sucursal)|retiro\s+en\s+tienda)\b', re.I)
SELECCION_RE = re.compile(r'(?:opci(?:o|ó)n\s*(\d+))|(?:\bla\s*(\d+)\b)|(?:n[uú]mero\s*(\d+))',re.I)
ADD_RE = re.compile(r'\b(agrega|agregar|añade|añadir|mete|pon(?:er)?|suma|agregalo|agregá|agregame)\b', re.I)
OFFTOPIC_RE = re.compile(r"(qué\s+vend[eé]n?|que\s+vend[eé]n?|qué\s+es\s+cassany|qu[eé]\s+es\s+cassany|d[oó]nde\s+est[aá]n|ubicaci[oó]n|horarios?|qu[ií]en(es)?\s+son|historia|c[oó]mo\s+funciona|pol[ií]tica(s)?\s+(de\s+)?(cambio|devoluci[oó]n|datos)|p[óo]liza|env[ií]os?\s*(nacionales|a\s+d[oó]nde)?|m[ée]todos?\s+de\s+pago)", re.I)
SMALLTALK_RE = re.compile(r"^(gracias|muchas gracias|ok|dale|listo|perfecto|bien|super|s[uú]per|genial|jaja+|jeje+|vale|de acuerdo|entendido|thanks|okey)\W*$", re.I)
DISCOVERY_RE = re.compile(r"(no\s*s[eé]\s*qu[eé]\s*comprar|qu[eé]\s+me\s+(recomiendas|sugieres)|recomi[eé]ndame|me\s+ayudas?\s+a\s+elegir|m(u|ú)estrame\s+opciones|quiero\s+ver\s+opciones|sugerencias|recomendaci[oó]n)", re.I)
CARRO_RE = re.compile(r'\b(carrito|mi carrito|ver carrito|ver el carrito|carro|mi pedido|resumen del pedido)\b', re.I)
MOSTRAR_RE = re.compile(r'\b(mu[eé]strame|muestrame|mostrarme|puedes mostrarme|puede mostrarme|podr[ií]as? mostrarme|quiero ver|ens[eñ]a(?:me)?)\b', re.I)
FOTOS_RE   = re.compile(r'\b(fotos?|im[aá]genes?)\s+de\s+([a-záéíóúñü\s]+)\b', re.I)

TALLA_RE = re.compile(r'\btalla\b|\b(XXL|XL|XS|S|M|L)\b', re.I)
NOMBRE_RE = re.compile(r'(?:me llamo|mi nombre es)\s*([a-záéíóúñü]+(?:\s+[a-záéíóúñü]+){1,3})', re.I)
TALLA_TOKEN_RE = re.compile(r'\b(XXL|XL|XS|S|M|L|28|30|32|34|36|38|40|42)\b', re.I)
USO_RE = re.compile(r'\b(oficina|formal|casual|evento|trabajo)\b', re.I)
MANGA_RE = re.compile(r'\bmanga\s+(corta|larga)\b', re.I)
COLOR_RE = re.compile(r'\b(blanco|blanca|negro|negra|azul|azules|beige|gris|rojo|verde|café|marr[oó]n|vinotinto|mostaza|crema|turquesa|celeste|lila|morado|rosa|rosado|amarillo|naranja)\b', re.I)

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

QTY_WORDS_MAP = {"un":1,"uno":1,"una":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,"seis":6,"siete":7,"ocho":8,"nueve":9,"diez":10}
QTY_WORD_RE = re.compile(r'\b(un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\b', re.I)
QTY_NUM_RE  = re.compile(r'\b(\d{1,2})\b')

def _norm_txt(s: str) -> str:
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

def extract_qty(texto: str) -> Optional[int]:
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
