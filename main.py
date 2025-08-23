# -*- coding: utf-8 -*-
"""
IAttom – WhatsApp Bot (Flask on Render)
- Conversa natural e empática
- Menu/ajuda com comandos úteis
- Memória simples por contato (RAM; opcional Redis)
- Bloqueio de respostas duplicadas (por msg_id)
- Webhook GET (verificação) + POST (mensagens)
"""

import os, re, time, json
from datetime import datetime
import requests
from flask import Flask, request

# ===================== 0) CONFIG =====================

ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN",    "COLE_SEU_LONG_LIVED_AQUI")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "730613666807699")
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN",    "testemax")  # deixe igual ao do Meta
BUSINESS_ID     = os.getenv("BUSINESS_ACCOUNT_ID", "")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")  # opcional (imagens)
REDIS_URL       = os.getenv("REDIS_URL", "")       # opcional (memória persistente)

GRAPH_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
HEADERS   = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

EMO_CHECKIN_HOURS = int(os.getenv("EMO_CHECKIN_HOURS", "6"))
FIRST_HEART = "💜"

# ===================== 1) MEMÓRIA =====================

PROFILE = {}   # wa_id -> info
PROCESSED = set()  # msg_ids já respondidos na vida do processo

rdb = None
if REDIS_URL:
    try:
        import redis
        rdb = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        print("⚠️ Redis não carregou, usando RAM:", e)
        rdb = None

def _now(): return time.time()

def mem_get(wa_id: str) -> dict:
    if rdb:
        raw = rdb.get(f"profile:{wa_id}")
        return json.loads(raw) if raw else {}
    return PROFILE.get(wa_id, {})

def mem_set(wa_id: str, data: dict):
    if rdb:
        rdb.set(f"profile:{wa_id}", json.dumps(data), ex=60*60*24*30)
    else:
        PROFILE[wa_id] = data

def mem_reset(wa_id: str):
    if rdb:
        rdb.delete(f"profile:{wa_id}")
    PROFILE.pop(wa_id, None)

# ===================== 2) HELPERS WhatsApp =====================

def send_text(wa_id: str, body: str):
    payload = {"messaging_product":"whatsapp","to":wa_id,"type":"text","text":{"body":body}}
    r = requests.post(GRAPH_URL, headers=HEADERS, json=payload, timeout=30)
    print("➡️ send_text:", r.status_code, r.text[:300])
    return r.ok

def send_image_link(wa_id: str, image_url: str, caption: str = ""):
    payload = {"messaging_product":"whatsapp","to":wa_id,"type":"image",
               "image":{"link":image_url,"caption":caption[:1024]}}
    r = requests.post(GRAPH_URL, headers=HEADERS, json=payload, timeout=30)
    print("➡️ send_image_link:", r.status_code, r.text[:300])
    return r.ok

def generate_image_url(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model":"gpt-image-1","prompt":prompt,"size":"1024x1024","n":1},
            timeout=60
        )
        if r.status_code == 200:
            data = r.json()
            return data["data"][0].get("url","") or ""
        print("⚠️ OpenAI image error:", r.status_code, r.text[:300])
    except Exception as e:
        print("⚠️ OpenAI image exception:", e)
    return ""

# ===================== 3) COMANDOS =====================

HELP_TEXT = (
    "Posso te ajudar com estes comandos:\n"
    "• *qual seu nome?* — eu me apresento\n"
    "• *ajuda* ou *menu* — mostra este menu\n"
    "• *foco* — técnica Pomodoro simples\n"
    "• *img: descrição* — tento gerar uma imagem (se houver OPENAI_API_KEY)\n"
    "• *diário: texto* — guardo uma anotação\n"
    "• *reset* — limpo suas preferências\n"
)

def normalize_display_name(raw: str) -> str:
    if not raw: return ""
    raw = re.sub(r"^\s*(hoje|today)[^A-Za-zÀ-ÿ]*", "", raw, flags=re.I)
    raw = re.sub(r"[,\s]{2,}", " ", raw).strip()
    first = raw.split()[0] if raw else ""
    return first.title()

def handle_commands(wa_id: str, text: str) -> bool:
    low = text.strip().lower()

    if low in ("ajuda", "menu", "help"):
        send_text(wa_id, HELP_TEXT)
        return True

    if low in ("foco", "pomodoro"):
        send_text(wa_id,
            "Vamos no simples: 25min de foco + 5min de pausa (Pomodoro).\n"
            "1) Escolha 1 tarefa pequena.\n"
            "2) Avise quando concluir a primeira — sigo com você.")
        return True

    if low in ("qual seu nome", "qual seu nome?", "seu nome", "seu nome?"):
        send_text(wa_id, "Eu sou o IAttom, o seu Assistente de Inteligência Artificial. 😊")
        return True

    if low.startswith("img:"):
        prompt = text[4:].strip()
        if not prompt:
            send_text(wa_id, "Diga o que quer desenhar: ex. *img: um pôr do sol em aquarela*.")
            return True
        url = generate_image_url(prompt)
        if url:
            send_image_link(wa_id, url, caption=f"IAttom – imagem: {prompt}")
        else:
            send_text(wa_id, "Consigo gerar imagens quando você definir sua OPENAI_API_KEY no Render.")
        return True

    if low.startswith("diário:") or low.startswith("diario:"):
        note = text.split(":",1)[1].strip() if ":" in text else ""
        p = mem_get(wa_id)
        diary = p.get("diary", [])
        if note:
            diary.append({"at": datetime.utcnow().isoformat()+"Z", "text": note})
            p["diary"] = diary
            mem_set(wa_id, p)
            send_text(wa_id, "Anotado no seu diário. 📘")
        else:
            if diary:
                last = diary[-1]["text"]
                send_text(wa_id, f"Sua última anotação foi: “{last}”.")
            else:
                send_text(wa_id, "Seu diário está vazio. Escreva: *diário: ...*")
        return True

    if low == "reset":
        mem_reset(wa_id)
        send_text(wa_id, "Pronto! Zerei suas preferências e memórias locais.")
        return True

    return False

# ===================== 4) RESPOSTAS NATURAIS =====================

NAME_PATTERNS = [
    r"(?:meu nome é|pode me chamar de|sou o|sou a)\s+([A-Za-zÀ-ÿ'´`^~\- ]{2,30})",
]

TRIGGERS_EMO  = ["triste","desanim","cansad","ansios","depress","sem vontade","sobrecarreg","estress"]
TRIGGERS_PROD = ["organizar","produtiv","foco","prioridade","planejar","agenda","projeto","procrast"]
TRIGGERS_STUD = ["estudar","prova","concurso","enem","vestibular","matéria","resumo","memoriz"]

SIGNOFF = "E lembre-se: você é capaz de muito mais do que imagina."

def extract_name_from_text(original: str) -> str|None:
    low = original.lower()
    for pat in NAME_PATTERNS:
        m = re.search(pat, low)
        if m:
            try:
                s, e = m.start(1), m.end(1)
                return original[s:e].strip().title()
            except:
                return m.group(1).strip().title()
    return None

def friendly_reply(wa_id: str, incoming_text: str) -> str:
    p = mem_get(wa_id)
    name = p.get("name")
    now  = _now()

    # aprender nome por frase
    new_name = extract_name_from_text(incoming_text)
    if new_name:
        name = new_name
        p["name"] = name
        mem_set(wa_id, p)
        return f"Prazer te conhecer, {name}! Pode contar comigo no que precisar."

    first_time = ("last_hi" not in p)
    if first_time:
        p["last_hi"] = now
        mem_set(wa_id, p)
        return ("Oi! Eu sou o IAttom — o seu Assistente de Inteligência Artificial. "
                "Como posso te chamar? " + FIRST_HEART)

    # gatilhos
    low = incoming_text.lower()
    if any(t in low for t in TRIGGERS_EMO):
        p["last_emo"] = now
        mem_set(wa_id, p)
        who = f"{name}, " if name else ""
        return (f"{who}eu tô com você. Respira um pouco comigo, tá? "
                "Se quiser, me conta o que está pesando — a gente quebra em passos pequenos. "
                + FIRST_HEART)

    if any(t in low for t in TRIGGERS_PROD):
        who = f"{name}, " if name else ""
        return (f"{who}vamos por partes: "
                "1) Liste 3 coisas que realmente importam hoje. "
                "2) Comece pela menor. "
                "3) Avise quando concluir a primeira — eu sigo com você.")

    if any(t in low for t in TRIGGERS_STUD):
        who = f"{name}, " if name else ""
        return (f"{who}estudo rende mais com blocos curtos: "
                "25min foco + 5min pausa (Pomodoro). Quer que eu te lembre?")

    who = f"{name}, " if name else ""
    return (f"{who}entendi. Me dá um pouquinho mais de contexto pra eu te ajudar melhor? "
            "Se for algo rápido, posso te sugerir o primeiro passo. " + FIRST_HEART)

# ===================== 5) FLASK / WEBHOOK =====================

app = Flask(__name__)

@app.route("/", methods=["GET"])
def root_ok():
    return "IAttom online ✅", 200

# Verificação do webhook
@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# Recebimento de mensagens
@app.route("/webhook", methods=["POST"])
def receive():
    data = request.get_json(silent=True, force=True)
    print("📥 evento:", json.dumps(data, ensure_ascii=False)[:800])

    try:
        entries = data.get("entry", [])
        if not entries: 
            return "OK", 200

        change = entries[0].get("changes", [])[0]
        value  = change.get("value", {})

        # Ignora eventos que não são mensagens (statuses/ack)
        if not value.get("messages"):
            return "OK", 200

        msg   = value["messages"][0]
        wa_id = msg.get("from")
        mtype = msg.get("type")
        msg_id = msg.get("id")

        # 🔒 evita duplicidade
        if msg_id in PROCESSED:
            print(f"⚠️ Mensagem {msg_id} já processada. Ignorando.")
            return "OK", 200
        PROCESSED.add(msg_id)

        # Atualiza nome a partir do profile (limpando status)
        contacts = value.get("contacts", [])
        if contacts and "profile" in contacts[0]:
            raw_name = contacts[0]["profile"].get("name", "")
            cleaned  = normalize_display_name(raw_name)
            if cleaned:
                p = mem_get(wa_id)
                if not p.get("name"):
                    p["name"] = cleaned
                    mem_set(wa_id, p)

        if mtype != "text":
            return "OK", 200

        body = (msg.get("text", {}) or {}).get("body", "").strip()
        print(f"💬 {wa_id}: {body}")

        # comandos
        if handle_commands(wa_id, body):
            return "OK", 200

        # resposta natural
        reply = friendly_reply(wa_id, body)
        send_text(wa_id, reply)

    except Exception as e:
        print("🔥 Erro no processamento:", repr(e))

    return "OK", 200

# ===================== 6) RUN (local) =====================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


