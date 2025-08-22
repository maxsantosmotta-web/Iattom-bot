# -*- coding: utf-8 -*-
"""
IAttom – WhatsApp Bot (Railway/Flask)
- Conversa natural, sem menu.
- Memória de nome por contato (RAM; opcional Redis).
- Tom empático + frases de incentivo.
- Gatilhos: saúde mental, produtividade, estudos, etc.
- Check-in periódico de bem-estar.
- Geração de imagens (opcional) via OpenAI (gpt-image-1) + envio por link.
- Webhook GET (verificação) / POST (mensagens).

>>> IMPORTANTE (segurança):
Em produção, troque os valores fixos por variáveis de ambiente no Railway:
  ACCESS_TOKEN, PHONE_NUMBER_ID, VERIFY_TOKEN, OPENAI_API_KEY, REDIS_URL
"""

import os, re, time, json, hmac, hashlib
from datetime import datetime, timedelta
import requests
from flask import Flask, request, jsonify

# ===================== 0) SEUS DADOS (já preenchidos) =====================

# Em produção, use os envs abaixo. Por agora, deixo os seus valores fixos.
# ACCESS_TOKEN     = os.getenv("ACCESS_TOKEN", "SEU_TOKEN_AQUI")
# PHONE_NUMBER_ID  = os.getenv("PHONE_NUMBER_ID", "ID_AQUI")
# VERIFY_TOKEN     = os.getenv("VERIFY_TOKEN", "SEGREDO_AQUI")
# BUSINESS_ID      = os.getenv("BUSINESS_ACCOUNT_ID", "")

ACCESS_TOKEN    = "EAAKdObY5RpQBPJiEa4GQnAwhk0KwvLHTv52KiuaZBtpH2ujSH7GZA6AmOZAWgGsfi0IIINY0fnCi7XJFcyF48SdWEvGvl9GeZBaJ99njQePJIgZAEauuTP1QUEH4ci4JMxwh0TZBnpZBdBKXQr1PFZBsF2U4gi7wcBfmV4PhRqB9CVrOjeZCdZAkWReTe9hKZCWcE7Oq72RPIYjtXwZD"
PHONE_NUMBER_ID = "730613666807699"
VERIFY_TOKEN    = "testemax"
BUSINESS_ID     = "1090223772743358"

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")   # opcional (para gerar imagem)
REDIS_URL       = os.getenv("REDIS_URL", "")        # opcional (memória persistente)

# Check-in de bem-estar a cada X horas (se não falar disso há Xh)
EMO_CHECKIN_HOURS = int(os.getenv("EMO_CHECKIN_HOURS", "6"))

GRAPH_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
HEADERS   = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Content-Type": "application/json"
}

# ===================== 1) MEMÓRIA =====================
# RAM (default). Se REDIS_URL existir, usamos Redis para persistir.
PROFILE = {}  # wa_id -> {"name": str|None, "last_hi": float, "last_emo": float}

rdb = None
if REDIS_URL:
    try:
        import redis
        rdb = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        print("⚠️ Redis não carregou, usando memória em RAM. Erro:", e)
        rdb = None

def _now_ts() -> float:
    return time.time()

def mem_get(wa_id: str) -> dict:
    if rdb:
        raw = rdb.get(f"profile:{wa_id}")
        if raw:
            return json.loads(raw)
        return {}
    return PROFILE.get(wa_id, {})

def mem_set(wa_id: str, data: dict):
    if rdb:
        rdb.set(f"profile:{wa_id}", json.dumps(data), ex=60*60*24*30)  # 30 dias
    else:
        PROFILE[wa_id] = data

# ===================== 2) HELPERS – WhatsApp =====================

def send_text(wa_id: str, body: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(GRAPH_URL, headers=HEADERS, json=payload, timeout=30)
    print("➡️ send_text:", r.status_code, r.text)
    return r.ok

def send_image_link(wa_id: str, image_url: str, caption: str = ""):
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "image",
        "image": {"link": image_url, "caption": caption[:1024]},
    }
    r = requests.post(GRAPH_URL, headers=HEADERS, json=payload, timeout=30)
    print("➡️ send_image_link:", r.status_code, r.text)
    return r.ok

# ===================== 3) GERAÇÃO DE IMAGEM (opcional) =====================

def generate_image_url(prompt: str) -> str:
    """
    Usa OpenAI Images (gpt-image-1) e retorna uma URL temporária.
    Precisa de OPENAI_API_KEY. Se não houver, retorna "".
    """
    if not OPENAI_API_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-image-1",
                "prompt": prompt,
                "size": "1024x1024",
                "n": 1
            },
            timeout=60
        )
        if resp.status_code == 200:
            data = resp.json()
            url = data["data"][0].get("url", "")
            return url or ""
        print("⚠️ OpenAI image error:", resp.status_code, resp.text)
    except Exception as e:
        print("⚠️ OpenAI image exception:", e)
    return ""

# ===================== 4) PERSONALIDADE / RESPOSTA =====================

NAME_PATTERNS = [
    r"(?:meu nome é|pode me chamar de|sou o|sou a)\s+([A-Za-zÀ-ÿ'´`^~\- ]{2,30})",
]

TRIGGERS_EMO  = ["triste", "desanim", "cansad", "ansios", "depress", "sem vontade", "sobrecarregado", "estress"]
TRIGGERS_PROD = ["organizar", "produtiv", "foco", "prioridade", "planejar", "agenda", "projeto", "procrast"]
TRIGGERS_STUD = ["estudar", "prova", "concurso", "enem", "vestibular", "matéria", "resumo", "memoriz"]

SIGNOFF = "E lembre-se: você é capaz de muito mais do que imagina. 💜"

def extract_name(msg: str) -> str | None:
    low = msg.lower()
    for pat in NAME_PATTERNS:
        m = re.search(pat, low)
        if m:
            # Captura nome como foi digitado (tente extrair do texto original)
            try:
                start = m.start(1)
                end   = m.end(1)
                return msg[start:end].strip().title()
            except:
                return m.group(1).strip().title()
    return None

def friendly_reply(wa_id: str, text: str) -> str:
    """
    Gera uma resposta amigável, usa nome quando souber,
    puxa check-in emocional se precisar e reage a gatilhos.
    """
    p = mem_get(wa_id)
    name = p.get("name")
    now  = _now_ts()

    # Aprender nome se a pessoa disser
    new_name = extract_name(text)
    if new_name:
        name = new_name
        p["name"] = name
        mem_set(wa_id, p)
        return f"Prazer te conhecer, {name}! Pode contar comigo no que precisar. {SIGNOFF}"

    first_time = ("last_hi" not in p)
    if first_time:
        p["last_hi"] = now
        mem_set(wa_id, p)

    # Check-in de bem-estar periódico
    last_emo = p.get("last_emo", 0)
    need_emo = (now - last_emo) > EMO_CHECKIN_HOURS * 3600

    # Saudação inicial
    if first_time:
        if not name:
            return ("Oi! Eu sou o IAttom — sua assistente de inteligência artificial. "
                    "Como posso te chamar? 🙂")
        else:
            return (f"Oi, {name}! Que bom te ver por aqui. Me conta: em que posso te ajudar hoje? "
                    f"{SIGNOFF}")

    # Gatilhos temáticos
    low = text.lower()
    if any(t in low for t in TRIGGERS_EMO):
        p["last_emo"] = now
        mem_set(wa_id, p)
        who = name or "hey"
        return (f"{who if name else 'Ei'}, eu tô com você. Respira um pouco comigo, tá? "
                "Se quiser, me conta o que está pesando — a gente quebra em passos pequenos. "
                f"{SIGNOFF}")

    if any(t in low for t in TRIGGERS_PROD):
        return (f"{name+', ' if name else ''}vamos por partes: "
                "1) Liste 3 coisas que realmente importam hoje. "
                "2) Comece pela menor. "
                "3) Avise quando concluir a primeira — eu sigo com você. 😉")

    if any(t in low for t in TRIGGERS_STUD):
        return (f"{name+', ' if name else ''}estudo flui melhor com ritmo curto: "
                "25min focado + 5min pausa (Técnica Pomodoro). "
                "Quer que eu te mande blocos de estudo?")

    # Puxar check-in se já passou do tempo
    if need_emo:
        p["last_emo"] = now
        mem_set(wa_id, p)
        return (f"{name+', ' if name else ''}como você tem se sentido hoje? "
                "Quero ter certeza de que você está bem. 💜")

    # Caso geral – conversa natural
    return (f"{name+', ' if name else ''}entendi. Me dá um pouquinho mais de contexto "
            "pra eu te ajudar melhor? Se for algo rápido, posso te sugerir o primeiro passo. "
            f"{SIGNOFF}")

# ===================== 5) FLASK / WEBHOOK =====================

app = Flask(__name__)

@app.route("/", methods=["GET"])
def root_ok():
    return "IAttom online ✅", 200

# Verificação do webhook (GET)
@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# Recebimento de mensagens (POST)
@app.route("/webhook", methods=["POST"])
def receive():
    data = request.get_json(silent=True, force=True)
    print("📥 evento:", json.dumps(data, ensure_ascii=False, indent=2))

    try:
        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value  = change.get("value", {})
        if value.get("messages"):
            msg = value["messages"][0]
            wa_id = msg["from"]

            # Atualiza perfil com nome do WhatsApp (se existir)
            contacts = value.get("contacts", [])
            if contacts and "profile" in contacts[0]:
                display_name = contacts[0]["profile"].get("name", "")
                if display_name:
                    p = mem_get(wa_id)
                    if not p.get("name"):
                        p["name"] = display_name.split()[0].title()
                        mem_set(wa_id, p)

            # Texto comum
            if msg.get("type") == "text":
                body = msg["text"]["body"].strip()

                # Gatilho para imagem: "img: descrição..."
                if body.lower().startswith("img:"):
                    prompt = body[4:].strip()
                    if not prompt:
                        send_text(wa_id, "Me diga o que você quer que eu desenhe: ex. `img: um pôr do sol em aquarela`.")
                    else:
                        url = generate_image_url(prompt)
                        if url:
                            send_image_link(wa_id, url, caption=f"IAttom – imagem: {prompt}")
                        else:
                            send_text(wa_id, "Posso gerar imagens se você configurar sua OPENAI_API_KEY no Railway. 😉")
                    return "OK", 200

                # Resposta natural
                reply = friendly_reply(wa_id, body)
                send_text(wa_id, reply)
    except Exception as e:
        print("⚠️ erro no processamento:", e)

    return "OK", 200

# ===================== 6) RUN (Railway) =====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
