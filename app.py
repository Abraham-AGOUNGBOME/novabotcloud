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
import json
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
Tu as accès à la mémoire (cibles, tarifs, apprentissages).

RÈGLES STRICTES :
1. Si l'administrateur te demande une information récente ou une tendance actuelle (ex: "tendances", "dernières news", "contacts", "donne-moi des sources"), tu DOIS utiliser l'outil search_web pour chercher sur le web. Ne réponds jamais sans avoir d'abord effectué une recherche si la question concerne des faits récents ou des données externes.
2. Si les résultats de recherche sont insuffisants, tu peux utiliser fetch_page pour lire le contenu des pages trouvées.
3. Compile toujours les résultats de manière structurée, avec les sources.
4. Si tu as déjà la réponse dans la mémoire (cibles, tarifs, apprentissages), utilise-la d'abord.
5. Quand c'est pertinent, termine par [MEMO:fichier] contenu pour sauvegarder automatiquement.
Sois concis et orienté action.
"""

CLIENT_PROMPT = """Tu es Larry, un assistant commercial amical et professionnel représentant NovaTech-IA, une entreprise spécialisée dans les visites virtuelles 3D pour l'immobilier, basée à Cotonou, Bénin.
Ton seul service : la création de visites virtuelles 3D immersives pour mettre en valeur des biens (appartements, villas, hôtels, résidences meublées).

Règles strictes :
- Reste toujours concentré sur la visite 3D. Ne mentionne jamais d'autres services (bots Telegram, automatisation IA, etc.) sauf si le client le demande explicitement.
- Écoute d'abord, pose des questions sur le bien à valoriser, son emplacement, son standing.
- Ne mentionne pas les prix sauf si le client te les demande. Tarifs : scan 3D standard à 75 000 FCFA, premium à 120 000 FCFA.
- Ne propose jamais de devis ni de rendez-vous. Si le client en veut un, réponds poliment que tu vas vérifier et revenir vers lui.
- Utilise l'historique pour ne pas répéter les mêmes questions.
- Quand tu as suffisamment d'informations (type de bien, localisation, budget évoqué, intérêt), produis un résumé pour l'administrateur avec la balise [RESUME]...[/FIN RESUME]. Le client ne doit jamais voir cette balise.
- Ne mentionne jamais les commandes admin, la mémoire, ou les coulisses techniques.
- Sois chaleureux mais concis. Parle comme un conseiller local, avec des références aux quartiers de Cotonou (Fidjrossè, Haie Vive, etc.) quand c'est pertinent.
"""

# ================= OUTILS AGENT =================
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

def fetch_page(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:3000] if text else "Page vide"
    except Exception as e:
        return f"Erreur récupération page : {str(e)}"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Recherche sur le web via DuckDuckGo et renvoie une liste de résultats (titre, extrait, URL).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "La requête de recherche"},
                    "max_results": {"type": "integer", "description": "Nombre maximum de résultats (défaut 5)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Récupère le contenu textuel d'une page web à partir de son URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "L'URL de la page à récupérer"}
                },
                "required": ["url"]
            }
        }
    }
]

def execute_function_call(function_name, arguments):
    if function_name == "search_web":
        query = arguments.get("query", "")
        max_results = arguments.get("max_results", 5)
        return search_web(query, max_results)
    elif function_name == "fetch_page":
        url = arguments.get("url", "")
        return fetch_page(url)
    else:
        return {"error": f"Fonction inconnue : {function_name}"}

# ================= GESTION DES CONVERSATIONS =================
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

# ================= GROQ AVEC GESTION 429 =================
def call_groq_with_tools(messages, tools=None, max_iterations=3):
    for i in range(max_iterations):
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "llama-3.1-8b-instant",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1000
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 429:
                time.sleep(5)
                continue
            resp.raise_for_status()
            data = resp.json()
            message = data["choices"][0]["message"]

            if "tool_calls" in message:
                messages.append(message)
                for tool_call in message["tool_calls"]:
                    function_name = tool_call["function"]["name"]
                    arguments = json.loads(tool_call["function"]["arguments"])
                    result = execute_function_call(function_name, arguments)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": function_name,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
                time.sleep(2)
                continue
            else:
                return message["content"]
        except Exception as e:
            if "429" in str(e):
                time.sleep(5)
                continue
            return f"Erreur API Groq : {str(e)}"
    return "Désolé, je n'ai pas pu accomplir la tâche (limite de tentatives atteinte)."

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
        
        # Premier essai avec outils
        response = call_groq_with_tools(messages, TOOLS)
        
        # Si la réponse ne semble pas contenir de recherche alors que c'est nécessaire, on force
        low_user = user_text.lower()
        low_resp = response.lower()
        if any(kw in low_user for kw in ["tendance", "contact", "source", "cherche", "trouve", "donne-moi", "scrape"]) \
           and "http" not in low_resp and "source" not in low_resp:
            # Relancer avec une instruction explicite
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": "Utilise immédiatement l'outil search_web pour chercher cette information. Donne les résultats avec les URLs."})
            response = call_groq_with_tools(messages, TOOLS)
        
        return response
    else:
        if pending_admin_chat_id == chat_id:
            return None
        history = get_conversation_context(chat_id)
        system_prompt = CLIENT_PROMPT
        if history:
            system_prompt += f"\n\nHistorique de la conversation :\n{history}\n\nRéponds en tenant compte de ce contexte."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ]
        try:
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "llama-3.1-8b-instant",
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 1000
            }
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return f"Erreur API Groq : {str(e)}"

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
    global pending_admin_chat_id
    if "[RESUME]" in response_text:
        admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
        if admin_chat_id:
            match = re.search(r"\[RESUME\](.*?)\[FIN RESUME\]", response_text, re.DOTALL)
            if match:
                resume = match.group(1).strip()
                nouveaute = detecter_nouveaute(resume)
                msg = f"📩 Résumé prospect :\n{resume}"
                if nouveaute:
                    msg += f"\n\n🆕 Nouveauté détectée : {nouveaute}\nSouhaitez-vous l'ajouter ? (/save ...)"
                msg += f"\n\n💬 Pour répondre au prospect, utilisez /dire <message>"
                bot.send_message(admin_chat_id, msg)
                pending_admin_chat_id = chat_id
    return response_text

def detecter_nouveaute(resume_text):
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

# ================= SCRAPING IA =================
def scrape_annonces():
    url = "https://keur-immo.com/benin"
    try:
        page_text = fetch_page(url)
        if page_text.startswith("Erreur"):
            return page_text
        prompt = f"""
Voici le contenu textuel de la page {url} (site d'annonces immobilières au Bénin).
Extrais les annonces qui concernent des appartements, villas ou résidences meublées situés à Cotonou (quartiers : Fidjrossè, Haie Vive, Cadjehoun, Ganhi, Zongo, Akpakpa).
Pour chaque annonce, donne le titre, le prix, la localisation et le lien (si trouvable).
Format :
🏠 Titre
💰 Prix
📍 Localisation
🔗 Lien
Si aucune annonce ne correspond, réponds "Aucune annonce pertinente trouvée."
Contenu de la page :
{page_text[:4000]}
"""
        messages = [{"role": "user", "content": prompt}]
        return call_groq_with_tools(messages, tools=[])
    except Exception as e:
        return f"Erreur scraping IA : {str(e)}"

def job_quotidien():
    chat_id = os.environ.get("ADMIN_CHAT_ID")
    if not chat_id:
        return
    rapport = scrape_annonces()
    bot.send_message(chat_id, f"📊 Rapport quotidien :\n{rapport}")

scheduler.add_job(job_quotidien, 'cron', hour=7, minute=0, timezone='Africa/Porto-Novo')

# ================= COMMANDES TELEGRAM =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if is_authorized(message):
        bot.reply_to(message, """👑 Mode Administrateur Larry
Commandes : /mem, /save, /scrape, /search, /dire, /pc
Larry est en ligne pour les clients.
Pour une mission complexe, décrivez simplement ce que vous voulez (ex: 'trouve-moi 5 contacts de promoteurs à Cotonou').""")
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
    results = search_web(query, 3)
    if isinstance(results, list) and len(results) > 0:
        reponse = ""
        for i, r in enumerate(results, 1):
            reponse += f"{i}. {r.get('title', 'Sans titre')}\n{r.get('snippet', '')}\n{r.get('url', '')}\n\n"
        bot.reply_to(message, reponse or "Aucun résultat trouvé.")
    else:
        bot.reply_to(message, "Erreur de recherche.")

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
        bot.reply_to(message, "Aucun prospect en attente de réponse.")
        return

    bot.send_message(pending_admin_chat_id, text)
    bot.reply_to(message, f"✅ Message envoyé au prospect {pending_admin_chat_id}.")
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

    bot.send_chat_action(message.chat.id, 'typing')
    response = process_message(message.text, chat_id, is_admin)

    if response is None:
        return

    if is_admin:
        response = handle_memo_action(response)
    else:
        conversations[chat_id].append(("bot", response))
        response_clean = re.sub(r"\[RESUME\].*?\[FIN RESUME\]", "", response, flags=re.DOTALL).strip()
        handle_resume_action(response, chat_id)
        response = response_clean

    bot.reply_to(message, response)

# ================= ROUTES FLASK =================
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
