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
# Vídeo enviado a quem passa na triagem (opcional)
# Prefira WHATSAPP_VIDEO_MEDIA_ID (upload feito uma vez para a Meta).
# Se não tiver, dá pra usar WHATSAPP_VIDEO_URL (link público direto do vídeo).
# Se nenhuma das duas estiver definida, o bot simplesmente não envia vídeo.
# ---------------------------------------------------------------------------
WHATSAPP_VIDEO_MEDIA_ID = os.environ.get("WHATSAPP_VIDEO_MEDIA_ID")
WHATSAPP_VIDEO_URL = os.environ.get("WHATSAPP_VIDEO_URL")
VIDEO_CAPTION = "Assista esse vídeo com as orientações antes de agendar sua consulta 🎬"

TRIGGER_WORDS = ("formulario", "questionario", "form", "cadastro", "pesquisa")
CANCEL_WORDS = ("cancelar", "sair", "parar")

# ---------------------------------------------------------------------------
# Perguntas de triagem (critérios de inclusão/exclusão da pesquisa)
# ---------------------------------------------------------------------------
# Cada pergunta tem:
#   key      -> nome da coluna no Supabase/planilha
#   prompt   -> texto da pergunta
#   options  -> lista de botões [{"id", "title", "exclude"}]
#               "exclude": True significa que, se o usuário escolher essa opção,
#               ele é removido da pesquisa (isso NUNCA aparece pro usuário,
#               é só uma marcação interna).
SCREENING_QUESTIONS = [
    {
        "key": "lingua_presa",
        "prompt": "Seu bebê foi diagnosticado com língua presa (anquiloglossia)?",
        "options": [
            {"id": "sim", "title": "Sim", "exclude": False},
            {"id": "nao", "title": "Não", "exclude": True},
        ],
    },
    {
        "key": "ate_2_meses",
        "prompt": "Seu bebê tem até 2 meses de vida?",
        "options": [
            {"id": "sim", "title": "Sim", "exclude": False},
            {"id": "nao", "title": "Não", "exclude": True},
        ],
    },
    {
        "key": "tipo_gravidez",
        "prompt": "Qual foi o tipo de gravidez?",
        "options": [
            {"id": "unica", "title": "Única", "exclude": False},
            {"id": "dupla_mais", "title": "Dupla ou mais", "exclude": True},
        ],
    },
    {
        "key": "pretende_amamentar",
        "prompt": "Você pretende continuar a dar de mamar com leite do seu peito?",
        "options": [
            {"id": "sim", "title": "Sim", "exclude": False},
            {"id": "nao", "title": "Não", "exclude": True},
        ],
    },
    {
        "key": "doencas_congenitas",
        "prompt": "O bebê possui doenças congênitas, malformações ou fenda?",
        "options": [
            {"id": "sim", "title": "Sim", "exclude": True},
            {"id": "nao", "title": "Não", "exclude": False},
        ],
    },
    {
        "key": "uti",
        "prompt": "O bebê esteve em UTI ou Unidade Intermediária?",
        "options": [
            {"id": "sim", "title": "Sim", "exclude": True},
            {"id": "nao", "title": "Não", "exclude": False},
        ],
    },
    {
        "key": "cirurgia_lingua",
        "prompt": "Seu bebê já passou por alguma cirurgia na língua?",
        "options": [
            {"id": "sim", "title": "Sim", "exclude": True},
            {"id": "nao", "title": "Não", "exclude": False},
        ],
    },
    {
        "key": "vitamina_k",
        "prompt": "Seu bebê tomou vitamina K na maternidade?",
        "options": [
            {"id": "sim", "title": "Sim", "exclude": False},
            {"id": "nao", "title": "Não", "exclude": True},
        ],
    },
]

EXCLUSION_MESSAGE = (
    "Seu bebê não atende aos critérios de inclusão desta pesquisa.\n"
    "Por favor, entre em contato pelo telefone (21) XXXXX-XXXX."
)

# ---------------------------------------------------------------------------
# Opções de data/horário para agendar a consulta (depois do vídeo)
# TROQUE pelos horários reais disponíveis. O "id" é só um identificador
# interno; o "title" é o que a pessoa vê (máx. 24 caracteres).
# ---------------------------------------------------------------------------
APPOINTMENT_OPTIONS = [
    {"id": "data_1", "title": "Seg 10/08 - 09h"},
    {"id": "data_2", "title": "Ter 11/08 - 14h"},
    {"id": "data_3", "title": "Qua 12/08 - 10h"},
    {"id": "data_4", "title": "Qui 13/08 - 15h"},
]

# Estado das conversas em andamento.
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
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            interactive_type = interactive.get("type")

            if interactive_type == "button_reply":
                button_id = interactive["button_reply"]["id"]
                handle_screening_reply(from_number, button_id)
            elif interactive_type == "list_reply":
                row_id = interactive["list_reply"]["id"]
                row_title = interactive["list_reply"]["title"]
                handle_appointment_reply(from_number, row_id, row_title)
            else:
                logger.info("Tipo de interactive não tratado: %s", interactive_type)
        else:
            # outros tipos: image, audio, location, etc.
            logger.info("Tipo de mensagem não tratado: %s", msg_type)
            send_text_message(from_number, "Por enquanto só entendo mensagens de texto 🙂")

    except (KeyError, IndexError) as e:
        logger.warning("Payload sem mensagem processável: %s", e)

    # A Meta exige resposta 200 rápida, senão reenvia o webhook
    return jsonify(status="ok"), 200


# ---------------------------------------------------------------------------
# Lógica do bot
# ---------------------------------------------------------------------------
# Estágios do fluxo, guardados em state["stage"]:
#   "screening"   -> perguntas de triagem com botões (Sim/Não), uma por vez
#   "scheduling"  -> lista de horários pra marcar a consulta (só quem passou)
def handle_text_message(from_number: str, text: str):
    text_stripped = text.strip()
    text_lower = text_stripped.lower()

    # Usuário já está no meio da pesquisa: nessas etapas só esperamos cliques
    # em botão/lista, não texto livre.
    if from_number in conversation_state:
        if text_lower in CANCEL_WORDS:
            conversation_state.pop(from_number, None)
            send_text_message(from_number, "Ok, cancelei o preenchimento. Digite *pesquisa* quando quiser recomeçar.")
            return

        send_text_message(from_number, "Por favor, toque em uma das opções da mensagem acima. Digite *cancelar* se quiser interromper.")
        return

    if text_lower in TRIGGER_WORDS:
        start_screening(from_number)
    elif text_lower in ("oi", "ola", "olá", "start", "menu"):
        send_text_message(
            from_number,
            "Olá! 👋 Eu sou o bot. Digite:\n"
            "1 - Ver planos\n"
            "2 - Falar com suporte\n"
            "3 - Participar da pesquisa",
        )
    elif text_lower == "1":
        send_text_message(from_number, "Aqui estão nossos planos... (placeholder)")
    elif text_lower == "2":
        send_text_message(from_number, "Encaminhando para o suporte... (placeholder)")
    elif text_lower == "3":
        start_screening(from_number)
    else:
        send_text_message(from_number, f"Recebi: {text}")


def start_screening(from_number: str):
    conversation_state[from_number] = {"stage": "screening", "step": 0, "answers": {}}
    send_text_message(from_number, "Vamos começar! Você pode digitar *cancelar* a qualquer momento.")
    ask_screening_question(from_number, 0)


def ask_screening_question(from_number: str, step: int):
    question = SCREENING_QUESTIONS[step]
    send_button_message(
        from_number,
        body_text=question["prompt"],
        buttons=[{"id": opt["id"], "title": opt["title"]} for opt in question["options"]],
    )


def handle_screening_reply(from_number: str, button_id: str):
    state = conversation_state.get(from_number)

    if not state or state.get("stage") != "screening":
        logger.info("Clique em botão ignorado (fora de contexto) de %s: %s", from_number, button_id)
        return

    step = state["step"]
    question = SCREENING_QUESTIONS[step]

    option = next((opt for opt in question["options"] if opt["id"] == button_id), None)
    if option is None:
        logger.warning("ID de botão desconhecido recebido de %s: %s", from_number, button_id)
        return

    state["answers"][question["key"]] = option["title"]

    if option["exclude"]:
        finish_as_excluded(from_number, state, failed_question_key=question["key"])
        return

    next_step = step + 1

    if next_step < len(SCREENING_QUESTIONS):
        state["step"] = next_step
        ask_screening_question(from_number, next_step)
    else:
        # Passou em todos os critérios: manda o vídeo e parte pro agendamento
        state["stage"] = "scheduling"
        send_completion_video(from_number)
        send_appointment_list(from_number)


def finish_as_excluded(from_number: str, state: dict, failed_question_key: str):
    conversation_state.pop(from_number, None)
    send_text_message(from_number, EXCLUSION_MESSAGE)

    # Registro opcional de quem foi excluído e por qual critério, útil pra
    # acompanhamento. Se preferir não guardar nada de quem não se enquadra,
    # é só remover este bloco.
    try:
        answers = dict(state["answers"])
        answers["status"] = "excluido"
        answers["motivo_exclusao"] = failed_question_key
        save_answers(from_number, answers)
    except Exception:
        logger.exception("Erro ao registrar exclusão de %s", from_number)


def send_appointment_list(from_number: str):
    send_list_message(
        from_number,
        body_text="Ótimo, seu bebê se enquadra na pesquisa! Escolha o melhor horário para sua consulta:",
        button_text="Ver horários",
        section_title="Datas disponíveis",
        rows=APPOINTMENT_OPTIONS,
    )


def handle_appointment_reply(from_number: str, row_id: str, row_title: str):
    state = conversation_state.get(from_number)

    if not state or state.get("stage") != "scheduling":
        logger.info("Escolha de horário ignorada (fora de contexto) de %s: %s", from_number, row_id)
        return

    state["answers"]["status"] = "concluido"
    state["answers"]["data_consulta"] = row_title

    conversation_state.pop(from_number, None)

    try:
        save_answers(from_number, state["answers"])
        send_text_message(
            from_number,
            f"Consulta agendada para *{row_title}*. Obrigado por participar! ✅",
        )
    except Exception:
        logger.exception("Erro ao salvar agendamento de %s", from_number)
        send_text_message(
            from_number,
            "Recebemos sua escolha de horário, mas houve um erro ao salvar. Nossa equipe já foi avisada.",
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


def send_button_message(to: str, body_text: str, buttons: list):
    """
    Envia uma mensagem com até 3 botões de resposta rápida.
    `buttons` é uma lista de dicts: [{"id": "...", "title": "..."}, ...]
    O título de cada botão tem limite de 20 caracteres (regra da própria API do WhatsApp).
    """
    if len(buttons) > 3:
        raise ValueError("A API do WhatsApp permite no máximo 3 botões por mensagem.")

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons
                ]
            },
        },
    }

    resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=10)

    if resp.status_code != 200:
        logger.error("Erro ao enviar botões: %s - %s", resp.status_code, resp.text)
    else:
        logger.info("Botões enviados para %s", to)

    return resp


def send_list_message(to: str, body_text: str, button_text: str, section_title: str, rows: list):
    """
    Envia uma mensagem com lista de opções (até 10 itens), útil quando
    são mais de 3 alternativas (limite dos botões simples).
    `rows` é uma lista de dicts: [{"id": "...", "title": "..."}, ...]
    Título de cada linha tem limite de 24 caracteres (regra da API do WhatsApp).
    """
    if len(rows) > 10:
        raise ValueError("A API do WhatsApp permite no máximo 10 itens numa lista.")

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_text[:20],
                "sections": [
                    {
                        "title": section_title[:24],
                        "rows": [
                            {"id": r["id"], "title": r["title"][:24]}
                            for r in rows
                        ],
                    }
                ],
            },
        },
    }

    resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=10)

    if resp.status_code != 200:
        logger.error("Erro ao enviar lista: %s - %s", resp.status_code, resp.text)
    else:
        logger.info("Lista enviada para %s", to)

    return resp


def send_completion_video(from_number: str):
    """Envia o vídeo de orientação, se um media_id ou URL estiver configurado."""
    try:
        if WHATSAPP_VIDEO_MEDIA_ID:
            send_video_message(from_number, media_id=WHATSAPP_VIDEO_MEDIA_ID, caption=VIDEO_CAPTION)
        elif WHATSAPP_VIDEO_URL:
            send_video_message(from_number, link=WHATSAPP_VIDEO_URL, caption=VIDEO_CAPTION)
        else:
            logger.info("Nenhum vídeo configurado (WHATSAPP_VIDEO_MEDIA_ID / WHATSAPP_VIDEO_URL) - pulando envio.")
    except Exception:
        # Falha ao mandar o vídeo não deve travar o fluxo de agendamento.
        logger.exception("Erro ao enviar vídeo de conclusão para %s", from_number)


def send_video_message(to: str, caption: str = "", media_id: str = None, link: str = None):
    """
    Envia um vídeo. Use `media_id` (preferido, upload já feito para a Meta)
    ou `link` (URL pública direta do arquivo de vídeo).
    """
    if not media_id and not link:
        raise ValueError("É preciso informar media_id ou link.")

    video_payload = {"caption": caption}
    if media_id:
        video_payload["id"] = media_id
    else:
        video_payload["link"] = link

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "video",
        "video": video_payload,
    }

    resp = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=15)

    if resp.status_code != 200:
        logger.error("Erro ao enviar vídeo: %s - %s", resp.status_code, resp.text)
    else:
        logger.info("Vídeo enviado para %s", to)

    return resp


# ---------------------------------------------------------------------------
# Health check (útil para o cron-job.org fazer ping e evitar hibernação)
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health_check():
    return jsonify(status="alive"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))