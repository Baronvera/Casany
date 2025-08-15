# utils_mensaje_whatsapp.py

from datetime import datetime

def _fmt_money(valor):
    try:
        return f"${float(valor):,.0f}"
    except Exception:
        return str(valor) if valor is not None else "-"

def generar_mensaje_atencion_humana(pedido):
    # Campos seguros
    nombre   = (pedido.nombre_cliente or "[Sin nombre]").strip()
    telefono = (pedido.telefono or "[Sin número]").strip()
    producto = (pedido.producto or "[Sin producto]").strip()
    talla    = (pedido.talla or "-").strip()
    cantidad = int(pedido.cantidad or 0)
    precio_u = float(pedido.precio_unitario or 0)
    subtotal = float(pedido.subtotal) if getattr(pedido, "subtotal", None) not in (None, "") else (precio_u * cantidad)

    # Entrega
    metodo_entrega = (pedido.metodo_entrega or "").strip().lower()
    if metodo_entrega == "domicilio":
        direccion = (pedido.direccion or "[Sin dirección]").strip()
        ciudad    = (pedido.ciudad or "").strip()
        entrega   = f"Envío a domicilio: {direccion}" + (f", {ciudad}" if ciudad else "")
    elif metodo_entrega == "recoger_en_tienda":
        tienda  = (getattr(pedido, "punto_venta", None) or "[Sin tienda]").strip()
        entrega = f"Recoger en tienda: {tienda}"
    else:
        entrega = "[Por definir]"

    # Pago
    mp = (pedido.metodo_pago or "").strip().lower()
    if mp == "transferencia":
        metodo_pago = "Transferencia"
    elif mp == "payu":
        metodo_pago = "PayU (online)"
    elif mp == "pago_en_tienda":
        metodo_pago = "Pago en tienda"
    else:
        metodo_pago = "[Por definir]"

    notas = (pedido.notas or "Ninguna").strip()
    if len(notas) > 600:
        notas = notas[:600] + "…"

    # Otros
    estado        = (pedido.estado or "pendiente").capitalize()
    confirmacion  = (getattr(pedido, "numero_confirmacion", None) or "[Aún sin confirmar]").strip()
    session_id    = (pedido.session_id or "-").strip()
    fecha_crea    = getattr(pedido, "fecha_creacion", None)
    if isinstance(fecha_crea, datetime):
        fecha_txt = fecha_crea.strftime("%Y-%m-%d %H:%M")
    else:
        fecha_txt = "-"

    return (
        "📌 *Pedido para atención humana*\n\n"
        f"*Fecha:* {fecha_txt}\n"
        f"*Sesión:* {session_id}\n"
        f"*Cliente:* {nombre} – {telefono}\n"
        f"*Producto:* {producto}\n"
        f"*Talla:* {talla}\n"
        f"*Cantidad:* {cantidad}\n"
        f"*Precio unitario:* {_fmt_money(precio_u)}\n"
        f"*Subtotal estimado:* {_fmt_money(subtotal)}\n"
        f"*Entrega:* {entrega}\n"
        f"*Método de pago:* {metodo_pago}\n"
        f"*Notas:* {notas}\n\n"
        f"*Número de confirmación:* {confirmacion}\n"
        f"*Estado:* {estado}"
    )
