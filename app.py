import os
import requests
import telebot
from flask import Flask
import threading
import subprocess
import time
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from collections import defaultdict
from datetime import datetime, timedelta
import re

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GIT_TOKEN = os.environ.get("GIT_TOKEN")
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

# ================= MÉMOIRE PERSISTANTE =================
MEMORY_DIR = "memory"
FILES = {
    "cibles": "cibles.md",
    "tarifs": "tarifs.md",
    "apprentissages": "apprentissages.md"
}
MEMORY = {}

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

def load_memory():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    for key, filename in FILES.items():
        filepath = os.path.join(MEMORY_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                MEMORY[key] = f.read()
        except FileNotFoundError:
            MEMORY[key] = ""

def save_memory(key):
    filepath = os.path.join(MEMORY_DIR, FILES[key])
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(MEMORY[key])
    git_push()

# ================= PROMPTS SYSTÈME =================
ADMIN_PROMPT = """Tu es NovaBot, l'assistant administratif de Larry (l'agent commercial de NovaTech-IA, Cotonou, Bénin).
Tu aides l'administrateur à gérer le business : analyse de marché, rédaction de messages de prospection, suivi des cibles.
Tu as accès à la mémoire (cibles, tarifs, apprentissages) et tu peux suggérer des actions.
Quand c'est pertinent, termine par [MEMO:fichier] contenu pour sauvegarder automatiquement.
Sois concis et orienté action.
"""

CLIENT_PROMPT = """Tu es Larry, un assistant commercial amical et professionnel représentant NovaTech-IA, une entreprise innovante de visites virtuelles 3D et d'automatisation basée à Cotonou, Bénin.
Ton rôle est d'écouter le client, de comprendre ses besoins et de le convaincre en douceur, sans jamais être insistant.

Règles strictes :
- Ne demande jamais explicitement le nom, le téléphone ou d'autres informations personnelles. Si le client les donne spontanément, tu peux les utiliser.
- Ne mentionne pas de prix sauf si le client le demande. Si on te demande un prix, tu donnes les tarifs standards (scan 3D standard 75 000 FCFA, premium 120 000 FCFA, hébergement 5 000 FCFA/mois).
- Ne propose jamais de devis ni de rendez-vous. Si le client en veut un, réponds poliment que tu vas vérifier la disponibilité et que tu reviens vers lui.
- Reste toujours dans le contexte de la conversation. Utilise l'historique fourni pour ne pas te répéter.
- Si le client semble prêt à acheter ou donne des informations claires sur son projet, tu prépares un résumé pour l'administrateur (que tu ne vois pas) en utilisant la balise spéciale [RESUME] (voir format plus bas).
- Ne mentionne jamais les commandes admin, la mémoire, ou les coulisses techniques.

Format du résumé (à n'utiliser que lorsque tu as suffisamment d'éléments) :
[RESUME]
Prospect : (prénom/nom si donné, sinon "inconnu")
Contact : (username Telegram si visible, sinon ID)
Besoin exprimé : ...
Budget évoqué ou réaction au prix : ...
Intérêt : (chaud/tiède/froid)
Nouveauté détectée : (décrire tout élément qui semble ne pas être dans la liste habituelle des cibles ou apprentissages – si tu n'as pas la mémoire, mets "inconnu")
[FIN RESUME]

Tu ne dois envoyer ce résumé qu'une seule fois, quand tu estimes avoir assez d'informations. Ensuite, tu attends les instructions (tu ne sais pas comment, c'est géré en coulisses).
"""

# ================= GESTION DES CONVERSATIONS =================
conversations = defaultdict(list)  # par chat_id, liste de (role, texte)
last_activity = {}                 # chat_id -> datetime du dernier message

def get_conversation_context(chat_id):
    """Retourne l'historique formaté pour le prompt."""
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

# ================= GROQ =================
def call_groq(messages):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1000
    }
    try:
        resp = requests.post(GROQ_URL, headers=headers, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Erreur API Groq : {str(e)}"

def process_message(user_text, chat_id, is_admin=False):
    if is_admin:
        mem_context = ""
        for key, content in MEMORY.items():
            if content.strip():
                mem_context += f"=== {key}.md ===\n{content}\n\n"
        messages = [
            {"role": "system", "content": ADMIN_PROMPT},
        ]
        if mem_context:
            messages.append({"role": "system", "content": f"Mémoire actuelle :\n{mem_context}"})
        messages.append({"role": "user", "content": user_text})
    else:
        history = get_conversation_context(chat_id)
        system_prompt = CLIENT_PROMPT
        if history:
            system_prompt += f"\n\nHistorique de la conversation :\n{history}\n\nRéponds en tenant compte de ce contexte."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ]
    return call_groq(messages)

def handle_memo_action(response_text):
    lines = response_text.split("\n")
    clean_lines = []
    for line in lines:
        if line.startswith("[MEMO:"):
            try:
                after_bracket = line[len("[MEMO:"):]
                key, value = after_bracket.split("]", 1)
                key = key.strip()
                if key in FILES:
                    MEMORY[key] += value.strip() + "\n"
                    save_memory(key)
            except:
                pass
        else:
            clean_lines.append(line)
    return "\n".join(clean_lines)

def handle_resume_action(response_text, chat_id):
    """Cherche un résumé dans la réponse de Larry et le transmet à l'admin."""
    if "[RESUME]" in response_text:
        admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
        if admin_chat_id:
            match = re.search(r"\[RESUME\](.*?)\[FIN RESUME\]", response_text, re.DOTALL)
            if match:
                resume = match.group(1).strip()
                # Détection nouveauté
                nouveaute = detecter_nouveaute(resume)
                msg = f"📩 Résumé prospect :\n{resume}"
                if nouveaute:
                    msg += f"\n\n🆕 Nouveauté détectée : {nouveaute}\nSouhaitez-vous l'ajouter ? (/save ...)"
                bot.send_message(admin_chat_id, msg)
                # Réinitialiser la conversation après résumé
                reset_conversation(chat_id)
    return response_text

def detecter_nouveaute(resume_text):
    """Compare le résumé avec cibles.md et apprentissages.md pour trouver des éléments nouveaux."""
    cibles = MEMORY.get("cibles", "").lower()
    apprentissages = MEMORY.get("apprentissages", "").lower()
    resume_lower = resume_text.lower()
    nouveautes = []
    mots_cles = ["quartier", "zone", "type de bien", "budget", "concurrent", "nouveau"]
    for mot in mots_cles:
        if mot in resume_lower:
            phrases = resume_lower.split(".")
            for phrase in phrases:
                if mot in phrase and phrase.strip() not in cibles and phrase.strip() not in apprentissages:
                    nouveautes.append(phrase.strip().capitalize())
    if nouveautes:
        return " ; ".join(nouveautes[:2])
    return None

# ================= SCRAPING =================
def scrape_annonces():
    # Placeholder – à adapter avec les vrais sélecteurs
    return "Scraping non configuré (sélecteurs à adapter)."

def job_quotidien():
    chat_id = os.environ.get("ADMIN_CHAT_ID")
    if not chat_id:
        return
    rapport = scrape_annonces()
    bot.send_message(chat_id, f"📊 Rapport quotidien :\n{rapport}")

scheduler.add_job(job_quotidien, 'cron', hour=7, minute=0, timezone='Africa/Porto-Novo')

# ================= RECHERCHE =================
def duckduckgo_search(query):
    try:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        abstract = data.get("AbstractText") or data.get("RelatedTopics", [{}])[0].get("Text", "")
        return abstract or "Aucune information trouvée."
    except Exception as e:
        return f"Erreur recherche : {e}"

# ================= COMMANDES TELEGRAM =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if is_authorized(message):
        bot.reply_to(message, """👑 Mode Administrateur Larry
Commandes : /mem, /save, /scrape, /search, /pc
Larry est en ligne pour les clients.""")
    else:
        username = message.chat.username or message.chat.first_name or "vous"
        bot.reply_to(message, f"Bonjour {username}, je suis Larry, conseiller chez NovaTech-IA. Comment puis-je vous aider aujourd'hui ?")

@bot.message_handler(commands=['mem'])
@authorized_only
def show_memory(message):
    text = "=== MÉMOIRE ===\n"
    for key, content in MEMORY.items():
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
        if key not in FILES:
            bot.reply_to(message, f"Fichiers : {', '.join(FILES.keys())}")
            return
        MEMORY[key] += text.strip() + "\n"
        save_memory(key)
        bot.reply_to(message, f"✅ Ajouté à {key}.md")
    except Exception as e:
        bot.reply_to(message, f"Erreur : {str(e)}")

@bot.message_handler(commands=['scrape'])
@authorized_only
def scrape_manuel(message):
    bot.send_chat_action(message.chat.id, 'typing')
    rapport = scrape_annonces()
    bot.reply_to(message, rapport)

@bot.message_handler(commands=['search'])
@authorized_only
def search_command(message):
    try:
        query = message.text.split(" ", 1)[1]
    except IndexError:
        bot.reply_to(message, "Usage : /search <mots-clés>")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    result = duckduckgo_search(query)
    bot.reply_to(message, f"🔍 Résultat : {result}")

@bot.message_handler(commands=['pc'])
@authorized_only
def pc_commands(message):
    bot.reply_to(message, "Commandes PC (à connecter) : /eteindre, /redemarrer, /ram, /screenshot")

# ================= HANDLER GÉNÉRAL =================
@bot.message_handler(func=lambda m: True)
def handle_all(message):
    chat_id = str(message.chat.id)
    is_admin = is_authorized(message)

    # Mise à jour de l'historique pour les clients
    if not is_admin:
        conversations[chat_id].append(("user", message.text))
        if len(conversations[chat_id]) > 10:
            conversations[chat_id] = conversations[chat_id][-10:]
        last_activity[chat_id] = datetime.now()

    bot.send_chat_action(message.chat.id, 'typing')
    response = process_message(message.text, chat_id, is_admin)

    if is_admin:
        response = handle_memo_action(response)
    else:
        conversations[chat_id].append(("bot", response))
        # Nettoyer la réponse de tout résumé avant de l'envoyer au client
        response_clean = re.sub(r"\[RESUME\].*?\[FIN RESUME\]", "", response, flags=re.DOTALL).strip()
        # Transmettre le résumé à l'admin si présent
        handle_resume_action(response, chat_id)
        response = response_clean

    bot.reply_to(message, response)

# ================= SERVEUR FLASK =================
@app.route('/')
def health():
    return 'Bot is running'

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# ================= DÉTECTION INACTIVITÉ =================
def check_inactive_conversations():
    now = datetime.now()
    for chat_id, last_time in list(last_activity.items()):
        if now - last_time > timedelta(minutes=5):
            admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
            if admin_chat_id:
                bot.send_message(admin_chat_id, f"⏰ Le prospect {chat_id} est inactif depuis 5 minutes. Pensez à vérifier.")
            reset_conversation(chat_id)

scheduler.add_job(check_inactive_conversations, 'interval', minutes=1)

# ================= LANCEMENT =================
if __name__ == '__main__':
    git_setup()
    git_pull()
    load_memory()
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    bot.infinity_polling()
