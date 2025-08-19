import os, json
from typing import Any, Dict
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from openai import OpenAI
from database import SessionLocal
from crud import crear_pedido, obtener_pedido_por_sesion
from agent_tools import TOOLS, SYSTEM_PROMPT, dispatch_tool

router = APIRouter(prefix="/agent", tags=["agent"])
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

class ChatIn(BaseModel):
    session_id: str
    message: str

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db)):
    sid, user_text = body.session_id, body.message
    if not obtener_pedido_por_sesion(db, sid):
        crear_pedido(db, {"session_id": sid, "estado": "pendiente", "carrito_json": "[]", "preferencias_json": "{}"})
    messages = [{"role":"system","content": SYSTEM_PROMPT},
                {"role":"user","content": user_text}]
    for _ in range(6):
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages, tools=TOOLS, tool_choice="auto", temperature=0.3
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                result = dispatch_tool(db, sid, name, args)
                messages.append({"role":"assistant","tool_calls":[{"id": tc.id,"type":"function",
                                "function":{"name":name,"arguments": json.dumps(args)}}]})
                messages.append({"role":"tool","tool_call_id": tc.id,"name": name,
                                 "content": json.dumps(result, ensure_ascii=False)})
            continue
        return {"response": msg.content or "¿Te muestro algunas opciones?"}
    return {"response": "No pude completar la acción. ¿Intentamos de nuevo?"}
