import os
import logging
import requests
from flask import Flask, request, jsonify

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-bot")

app = Flask(__name__)

WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]          # token de acesso (temporário ou permanente)
PHONE_NUMBER_ID = os.environ["PHONE_NUMBER_ID"]         # ID do número na API Setup
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]                # string qualquer, você escolhe (usada na verificação do webhook)
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v20.0")

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"

# ---------------------------------------------------------------------------
# Configuração do Supabase (armazenamento principal das respostas)
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]                 # ex: https://xxxx.supabase.co
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # service_role key (Settings > API no Supabase)
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "form_responses")

SUPABASE_REST_URL = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"

# ---------------------------------------------------------------------------
# Configuração do Google Apps Script (espelha as respostas numa planilha)
# Opcional: se GOOGLE_SCRIPT_URL não estiver definido, o bot só usa Supabase.
# ---------------------------------------------------------------------------
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")  # URL do Web App do Apps Script
GOOGLE_SCRIPT_SECRET = os.environ.get("GOOGLE_SCRIPT_SECRET")  # mesma string usada no script


# ---------------------------------------------------------------------------
# Definição do formulário/questionário
# ---------------------------------------------------------------------------
# Cada pergunta tem uma "key" (usada como coluna na tabela/planilha) e o texto
# enviado ao usuário. Adicione, remova ou reordene à vontade.
QUESTIONS = [
    {"key": "nome", "prompt": "Qual é o seu nome completo?"},
    {"key": "email", "prompt": "Qual é o seu e-mail?"},
    {"key": "cidade", "prompt": "Em qual cidade você mora?"},
]

TRIGGER_WORDS = ("formulario", "questionario", "form", "cadastro")
CANCEL_WORDS = ("cancelar", "sair", "parar")

# Estado das conversas em andamento: { numero: {"step": int, "answers": {...}} }
# ATENÇÃO: isso vive na memória do processo. Se o Render reiniciar o serviço
# ou rodar mais de 1 worker do gunicorn, o estado se perde/duplica.
# Seu Start Command já usa "-w 1" (1 worker), então está correto.
conversation_state = {}


# ---------------------------------------------------------------------------
# Verificação do webhook (GET) - a Meta chama isso uma vez ao configurar
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verificado com sucesso.")
        return challenge, 200

    logger.warning("Falha na verificação do webhook.")
    return "Forbidden", 403


# ---------------------------------------------------------------------------
# Recebimento de mensagens (POST) - toda mensagem do usuário chega aqui
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    logger.info("Payload recebido: %s", data)

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        # Ignora eventos que não são mensagens (ex: status de entrega/leitura)
        if "messages" not in value:
            return jsonify(status="ignored"), 200

        message = value["messages"][0]
        from_number = message["from"]  # número do usuário, formato internacional sem "+"
        msg_type = message.get("type")

        if msg_type == "text":
            text_body = message["text"]["body"]
            handle_text_message(from_number, text_body)
        else:
            # outros tipos: image, audio, interactive (botões/listas), location, etc.
            logger.info("Tipo de mensagem não tratado: %s", msg_type)
            send_text_message(from_number, "Por enquanto só entendo mensagens de texto 🙂")

    except (KeyError, IndexError) as e:
        logger.warning("Payload sem mensagem processável: %s", e)

    # A Meta exige resposta 200 rápida, senão reenvia o webhook
    return jsonify(status="ok"), 200


# ---------------------------------------------------------------------------
# Lógica do bot
# ---------------------------------------------------------------------------
def handle_text_message(from_number: str, text: str):
    text_stripped = text.strip()
    text_lower = text_stripped.lower()

    # Usuário já está no meio do formulário
    if from_number in conversation_state:
        if text_lower in CANCEL_WORDS:
            conversation_state.pop(from_number, None)
            send_text_message(from_number, "Ok, cancelei o preenchimento. Digite *formulario* quando quiser recomeçar.")
            return
        handle_form_answer(from_number, text_stripped)
        return

    if text_lower in TRIGGER_WORDS:
        start_form(from_number)
    elif text_lower in ("oi", "ola", "olá", "start", "menu"):
        send_text_message(
            from_number,
            "Olá! 👋 Eu sou o bot. Digite:\n"
            "1 - Ver planos\n"
            "2 - Falar com suporte\n"
            "3 - Preencher formulário",
        )
    elif text_lower == "1":
        send_text_message(from_number, "Aqui estão nossos planos... (placeholder)")
    elif text_lower == "2":
        send_text_message(from_number, "Encaminhando para o suporte... (placeholder)")
    elif text_lower == "3":
        start_form(from_number)
    else:
        send_text_message(from_number, f"Recebi: {text}")


def start_form(from_number: str):
    conversation_state[from_number] = {"step": 0, "answers": {}}
    send_text_message(from_number, "Vamos preencher o formulário! Você pode digitar *cancelar* a qualquer momento.")
    send_text_message(from_number, QUESTIONS[0]["prompt"])


def handle_form_answer(from_number: str, answer: str):
    state = conversation_state[from_number]
    step = state["step"]
    key = QUESTIONS[step]["key"]
    state["answers"][key] = answer

    next_step = step + 1

    if next_step < len(QUESTIONS):
        state["step"] = next_step
        send_text_message(from_number, QUESTIONS[next_step]["prompt"])
    else:
        # Última pergunta respondida: salva na planilha e encerra
        conversation_state.pop(from_number, None)
        try:
            save_answers(from_number, state["answers"])
            send_text_message(from_number, "Obrigado! Suas respostas foram registradas com sucesso. ✅")
        except Exception:
            logger.exception("Erro ao salvar respostas de %s", from_number)
            send_text_message(
                from_number,
                "Recebi suas respostas, mas houve um erro ao salvar. Nossa equipe já foi avisada.",
            )


def save_answers(from_number: str, answers: dict):
    """Salva as respostas no Supabase e, se configurado, espelha na planilha do Google."""
    save_to_supabase(from_number, answers)

    if GOOGLE_SCRIPT_URL:
        try:
            sync_to_google_sheet(from_number, answers)
            logger.info("Respostas de %s sincronizadas com o Google Sheets", from_number)
        except Exception:
            # Falha ao espelhar no Sheets não deve derrubar o fluxo: os dados
            # já estão seguros no Supabase.
            logger.exception("Falha ao sincronizar com o Google Sheets (dados já salvos no Supabase)")
    else:
        logger.info("GOOGLE_SCRIPT_URL não configurado - pulando sincronização com o Sheets")


def save_to_supabase(from_number: str, answers: dict):
    from datetime import datetime, timezone

    row = {
        "whatsapp_number": from_number,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **answers,
    }

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    resp = requests.post(SUPABASE_REST_URL, headers=headers, json=row, timeout=10)

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao salvar no Supabase: {resp.status_code} - {resp.text}")


def sync_to_google_sheet(from_number: str, answers: dict):
    from datetime import datetime, timezone

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "numero_whatsapp": from_number,
        **answers,
        "secret": GOOGLE_SCRIPT_SECRET,
    }
    resp = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=10)

    if resp.status_code != 200:
        raise RuntimeError(f"Erro ao chamar o Apps Script: {resp.status_code} - {resp.text}")


# ---------------------------------------------------------------------------
# Envio de mensagens (chamada à Cloud API)
# ---------------------------------------------------------------------------
def send_text_message(to: str, body: str):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }

    resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=10)

    if resp.status_code != 200:
        logger.error("Erro ao enviar mensagem: %s - %s", resp.status_code, resp.text)
    else:
        logger.info("Mensagem enviada para %s", to)

    return resp


# ---------------------------------------------------------------------------
# Health check (útil para o cron-job.org fazer ping e evitar hibernação)
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health_check():
    return jsonify(status="alive"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))