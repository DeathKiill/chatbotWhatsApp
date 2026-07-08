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
# Lógica do bot (troque isso pela sua lógica real: Supabase, planos, etc.)
# ---------------------------------------------------------------------------
def handle_text_message(from_number: str, text: str):
    text_lower = text.strip().lower()

    if text_lower in ("oi", "ola", "olá", "start", "menu"):
        send_text_message(
            from_number,
            "Olá! 👋 Eu sou o bot. Digite:\n"
            "1 - Ver planos\n"
            "2 - Falar com suporte",
        )
    elif text_lower == "1":
        send_text_message(from_number, "Aqui estão nossos planos... (placeholder)")
    elif text_lower == "2":
        send_text_message(from_number, "Encaminhando para o suporte... (placeholder)")
    else:
        send_text_message(from_number, f"Recebi: {text}")


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
