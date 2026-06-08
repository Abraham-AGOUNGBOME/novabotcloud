import os
import requests
import telebot
from flask import Flask, request
import threading
import subprocess
import time
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from collections import defaultdict
from datetime import datetime, timedelta
import re
from duckduckgo_search import DDGS

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GIT_TOKEN = os.environ.get("GIT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

logging.basicConfig(level=logging.INFO)

# ================= AUTORISATION =================
AUTHORIZED_IDS = os.environ.get("AUTHORIZED_USERS", "").split(",")
AUTHORIZED_IDS = [uid.strip() for uid in AUTHORIZED_IDS if uid.strip()]

def is_authorized(message):
    return str(message.chat.id) in AUTHORIZED_IDS

def authorized_only(func):
    def wrapper(message, *args, **kwargs):
        if not is_authorized(message):
            bot.reply_to(message, "⛔ Commande réservée à l'administrateur.")
            return
        return func(message, *args, **kwargs)
    return wrapper

# ================= MÉMOIRE PARTAGÉE =================
MEMORY_DIR = "memory"
SHARED_FILES = ["cibles.md", "tarifs.md", "apprentissages.md"]
SHARED_MEMORY = {}

def load_all_memory():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    for fname in SHARED_FILES:
        filepath = os.path.join(MEMORY_DIR, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                SHARED_MEMORY[fname.replace(".md", "")] = f.read()
        except FileNotFoundError:
            SHARED_MEMORY[fname.replace(".md", "")] = ""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("")

def save_shared_memory():
    for key, content in SHARED_MEMORY.items():
        filepath = os.path.join(MEMORY_DIR, f"{key}.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
    git_push()

# ================= GIT =================
def git_setup():
    repo_url = f"https://{GIT_TOKEN}@github.com/Abraham-AGOUNGBOME/novabotcloud.git"
    subprocess.run(["git", "remote", "set-url", "origin", repo_url], capture_output=True)
    subprocess.run(["git", "config", "user.email", "novabot@novatech.bj"], capture_output=True)
    subprocess.run(["git", "config", "user.name", "NovaBot"], capture_output=True)

def git_pull():
    subprocess.run(["git", "pull", "origin", "main"], capture_output=True)

def git_push():
    subprocess.run(["git", "add", f"{MEMORY_DIR}/*.md"], capture_output=True)
    commit_msg = f"Memory update {time.strftime('%Y-%m-%d %H:%M:%S')}"
    subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], capture_output=True)

# ================= RECHERCHE WEB =================
def search_web(query, max_results=5):
    try:
        with DDGS() as ddgs:
            results = []
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r["title"],
                    "snippet": r["body"],
                    "url": r["href"]
                })
            return results if results else [{"error": "Aucun résultat trouvé"}]
    except Exception as e:
        return [{"error": f"Erreur recherche : {str(e)}"}]

# ================= GESTION CONVERSATIONS CLIENT =================
conversations = defaultdict(list)
last_activity = {}
pending_admin_chat_id = None

def get_conversation_context(chat_id):
    msgs = conversations[chat_id]
    if not msgs:
        return ""
    lines = []
    for role, text in msgs:
        if role == "user":
            lines.append(f"Client : {text}")
        else:
            lines.append(f"Larry : {text}")
    return "\n".join(lines)

def reset_conversation(chat_id):
    conversations.pop(chat_id, None)
    last_activity.pop(chat_id, None)
    global pending_admin_chat_id
    if pending_admin_chat_id == chat_id:
        pending_admin_chat_id = None

# ================= PROMPTS =================
ADMIN_PROMPT = """Tu es NovaBot, assistant administratif de NovaTech-IA, Cotonou, Bénin.
Services : visites virtuelles 3D pour l'immobilier.
Tarifs officiels (les seuls que tu peux utiliser) :
- Scan 3D standard : 75 000 FCFA
- Scan 3D premium : 120 000 FCFA
- Hébergement mensuel : 5 000 FCFA/mois

Mémoire partagée (cibles, tarifs, apprentissages) : tu y as accès.
Si tu as besoin d'une information récente, tu peux utiliser l'outil search_web.
Ne mentionne jamais un autre pays que le Bénin.
Sois concis, professionnel, et n'invente jamais de prix ou de statistiques.
"""

LARRY_PROMPT = """Tu es Larry, conseiller commercial de NovaTech-IA, Cotonou, Bénin.
Spécialiste des visites virtuelles 3D pour l'immobilier.
Tonnels prix (à ne donner que si le client demande) :
- Scan 3D standard : 75 000 FCFA
- Scan 3D premium : 120 000 FCFA
- Hébergement mensuel : 5 000 FCFA/mois

Écoute, pose des questions sur le bien, ne force jamais.
Quand tu as assez d'infos, produis un résumé avec [RESUME]...[/FIN RESUME].
Ne parle pas d'autres services. Ne parle jamais de tarifs que ceux ci-dessus.
"""

# ================= APPEL GROQ SIMPLE =================
def simple_groq_call(messages, max_tokens=1000):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens
    }
    try:
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Erreur API Groq : {str(e)}"

# ================= TRAITEMENT ADMIN (avec recherche si nécessaire) =================
def process_admin_message(user_text):
    shared_context = ""
    for key, content in SHARED_MEMORY.items():
        if content.strip():
            shared_context += f"=== {key}.md ===\n{content}\n\n"

    # Si la demande contient un mot-clé de recherche, lancer search_web
    low_user = user_text.lower()
    if any(kw in low_user for kw in ["tendance", "contact", "source", "cherche", "trouve", "scrape", "actualité", "récent"]):
        results = search_web(user_text)
        if results and "error" not in results[0]:
            # Construire un résumé pour Groq
            results_str = json.dumps(results, ensure_ascii=False)[:2500]
            prompt = f"""Voici les résultats d'une recherche web pour "{user_text}" :
{results_str}
Rédige une réponse structurée avec les sources (URLs). Les tarifs à utiliser sont les officiels."""
            messages = [
                {"role": "system", "content": ADMIN_PROMPT},
                {"role": "system", "content": f"Mémoire :\n{shared_context}"},
                {"role": "user", "content": prompt}
            ]
            return simple_groq_call(messages)
        else:
            return "Désolé, la recherche web n'a donné aucun résultat."
    else:
        # Pas de recherche, réponse directe
        messages = [
            {"role": "system", "content": ADMIN_PROMPT},
            {"role": "system", "content": f"Mémoire :\n{shared_context}"},
            {"role": "user", "content": user_text}
        ]
        return simple_groq_call(messages)

# ================= TRAITEMENT CLIENT (Larry) =================
def process_client_message(user_text, chat_id):
    if pending_admin_chat_id == chat_id:
        return None
    history = get_conversation_context(chat_id)
    system_prompt = LARRY_PROMPT
    if history:
        system_prompt += f"\n\nHistorique :\n{history}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text}
    ]
    resp = simple_groq_call(messages)
    return resp

# ================= COMMANDES TELEGRAM =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if is_authorized(message):
        bot.reply_to(message, """👑 Mode Admin NovaBot
Commandes : /mem, /save, /scrape, /search, /dire, /pc""")
    else:
        username = message.chat.username or message.chat.first_name or "vous"
        bot.reply_to(message, f"Bonjour {username}, je suis Larry, conseiller chez NovaTech-IA. Comment puis-je vous aider ?")

@bot.message_handler(commands=['mem'])
@authorized_only
def show_memory(message):
    text = "=== MÉMOIRE ===\n"
    for key, content in SHARED_MEMORY.items():
        text += f"\n--- {key}.md ---\n{content or '(vide)'}"
    bot.reply_to(message, text[:4000])

@bot.message_handler(commands=['save'])
@authorized_only
def save_memory_command(message):
    try:
        parts = message.text.split(" ", 2)
        if len(parts) < 3:
            bot.reply_to(message, "Usage : /save <fichier> <texte>")
            return
        key = parts[1].lower()
        text = parts[2]
        if key not in SHARED_MEMORY:
            bot.reply_to(message, f"Fichiers : {', '.join(SHARED_MEMORY.keys())}")
            return
        SHARED_MEMORY[key] += text.strip() + "\n"
        save_shared_memory()
        bot.reply_to(message, f"✅ Ajouté à {key}.md")
    except Exception as e:
        bot.reply_to(message, f"Erreur : {str(e)}")

@bot.message_handler(commands=['scrape'])
@authorized_only
def scrape_manuel(message):
    bot.send_chat_action(message.chat.id, 'typing')
    results = search_web("annonces immobilières Cotonou")
    reponse = "Résultat du scraping :\n"
    for r in results[:5]:
        reponse += f"- {r['title']} ({r['url']})\n"
    bot.reply_to(message, reponse or "Aucun résultat.")

@bot.message_handler(commands=['search'])
@authorized_only
def search_command(message):
    try:
        query = message.text.split(" ", 1)[1]
    except IndexError:
        bot.reply_to(message, "Usage : /search <mots-clés>")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    results = search_web(query, 3)
    reponse = ""
    for i, r in enumerate(results, 1):
        reponse += f"{i}. {r['title']}\n{r['snippet']}\n{r['url']}\n\n"
    bot.reply_to(message, reponse or "Aucun résultat trouvé.")

@bot.message_handler(commands=['pc'])
@authorized_only
def pc_commands(message):
    bot.reply_to(message, "Commandes PC (à connecter) : /eteindre, /redemarrer, /ram, /screenshot")

@bot.message_handler(commands=['dire'])
@authorized_only
def dire_command(message):
    global pending_admin_chat_id
    try:
        text = message.text.split(" ", 1)[1]
    except IndexError:
        bot.reply_to(message, "Usage : /dire <message à envoyer au prospect>")
        return
    if pending_admin_chat_id is None:
        bot.reply_to(message, "Aucun prospect en attente.")
        return
    bot.send_message(pending_admin_chat_id, text)
    bot.reply_to(message, f"✅ Envoyé à {pending_admin_chat_id}.")
    pending_admin_chat_id = None

@bot.message_handler(func=lambda m: True)
def handle_all(message):
    chat_id = str(message.chat.id)
    is_admin = is_authorized(message)

    if not is_admin and pending_admin_chat_id == chat_id:
        bot.reply_to(message, "Veuillez patienter, un conseiller va vous répondre personnellement.")
        return

    if not is_admin:
        conversations[chat_id].append(("user", message.text))
        if len(conversations[chat_id]) > 10:
            conversations[chat_id] = conversations[chat_id][-10:]
        last_activity[chat_id] = datetime.now()
        response = process_client_message(message.text, chat_id)
        if response:
            conversations[chat_id].append(("bot", response))
            if "[RESUME]" in response:
                admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
                if admin_chat_id:
                    match = re.search(r"\[RESUME\](.*?)\[FIN RESUME\]", response, re.DOTALL)
                    if match:
                        resume = match.group(1).strip()
                        bot.send_message(admin_chat_id, f"📩 Résumé prospect :\n{resume}")
                        pending_admin_chat_id = chat_id
            response = re.sub(r"\[RESUME\].*?\[FIN RESUME\]", "", response, flags=re.DOTALL).strip()
        bot.reply_to(message, response)
    else:
        bot.send_chat_action(message.chat.id, 'typing')
        response = process_admin_message(message.text)
        bot.reply_to(message, response)

# ================= FLASK =================
@app.route('/', methods=['GET'])
def health():
    return 'Bot is running'

@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        return 'Bad request', 400

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# ================= INACTIVITÉ =================
def check_inactive_conversations():
    now = datetime.now()
    for chat_id, last_time in list(last_activity.items()):
        if now - last_time > timedelta(minutes=5):
            admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
            if admin_chat_id:
                bot.send_message(admin_chat_id, f"⏰ Prospect {chat_id} inactif depuis 5 min.")
            reset_conversation(chat_id)

scheduler.add_job(check_inactive_conversations, 'interval', minutes=1)

# ================= LANCEMENT =================
if __name__ == '__main__':
    git_setup()
    git_pull()
    load_all_memory()

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    if WEBHOOK_URL:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook configuré sur {WEBHOOK_URL}")
    else:
        logging.warning("WEBHOOK_URL non défini, utilisation du polling (non recommandé)")
        bot.infinity_polling()

    while True:
        time.sleep(60)
