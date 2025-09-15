# webhook.py
import os, json, hmac, hashlib
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Header
from sqlalchemy.orm import Session

from database import SessionLocal
from api_core import enviar_mensaje_whatsapp   # reusa envío saliente
from api_core import mensaje_whatsapp, UserMessage  # handler conversacional

router = APIRouter()

WA_APP_SECRET = os.getenv("WA_APP_SECRET", "")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")

def _verify_wa_signature(raw_body: bytes, signature_256: str) -> bool:
    if not WA_APP_SECRET:
        return True
    if not signature_256:
        return False
    try:
        mac = hmac.new(WA_APP_SECRET.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256)
        expected = "sha256=" + mac.hexdigest()
        return hmac.compare_digest(expected, signature_256)
    except Exception:
        return False

@router.get("/webhook")
def verify_webhook(
    hub_mode: str = None,
    hub_challenge: str = None,
    hub_verify_token: str = None,
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        from fastapi import Response
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(400, "Token de verificación inválido.")

@router.post("/webhook")
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
                continue
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
                db: Session = SessionLocal()
                try:
                    print(f"🧪 Texto recibido: {txt}")
                    res = await mensaje_whatsapp(UserMessage(message=txt), session_id=session_id, db=db)
                    await enviar_mensaje_whatsapp(num, res.get("response", ""))
                finally:
                    db.close()
    return {"status": "received"}
