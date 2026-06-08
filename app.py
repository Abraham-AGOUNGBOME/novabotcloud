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

# ================= MÉMOIRE PERSISTANTE MULTI-AGENTS =================
MEMORY_DIR = "memory"
AGENTS = {
    "NovaBot": {
        "prompt": """Tu es NovaBot, l'assistant administratif central de NovaTech-IA, Cotonou, Bénin.
Tu supervises les autres agents (Market, Créa, Coco, Larry) et tu prends les décisions finales.
Tu as accès à toutes les mémoires. Sois concis et orienté action.""",
        "memory_file": "novabot_memory.md",
        "description": "Assistant principal, gestion administrative, supervision"
    },
    "Market": {
        "prompt": """Tu es Market, l'agent d'analyse de marché et de tendances.
Tu cherches sur le web, analyses les données, et fournis des rapports avec sources.
Utilise l'outil search_web quand nécessaire. Sois précis et chiffré.""",
        "memory_file": "market_memory.md",
        "description": "Recherche de tendances, analyse de marché, veille concurrentielle"
    },
    "Créa": {
        "prompt": """Tu es Créa, l'agent créatif de NovaTech-IA.
Tu rédiges des posts Facebook, des messages de prospection, des accroches marketing.
Ton style est punchy, moderne, adapté au public béninois. Pas de blabla corporate.""",
        "memory_file": "crea_memory.md",
        "description": "Création de contenu, rédaction marketing, posts réseaux sociaux"
    },
    "Coco": {
        "prompt": """Tu es Coco, le comptable de NovaTech-IA.
Tu gères les tarifs, les devis, la rentabilité. Tu calcules les marges et suis les revenus.
Sois rigoureux et transparent.""",
        "memory_file": "coco_memory.md",
        "description": "Comptabilité, tarifs, devis, suivi financier"
    },
    "Larry": {
        "prompt": """Tu es Larry, l'agent commercial pour les visites 3D.
Tu parles aux clients potentiels, tu les qualifies, tu ne forces jamais.
Ne mentionne pas les autres agents. Reste chaleureux et professionnel.""",
        "memory_file": "larry_memory.md",
        "description": "Vente et qualification de prospects pour visites 3D"
    }
}
# Mémoire chargée pour chaque agent
MEMORY = {}

def load_all_memory():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    for agent_name, cfg in AGENTS.items():
        filepath = os.path.join(MEMORY_DIR, cfg["memory_file"])
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                MEMORY[agent_name] = f.read()
        except FileNotFoundError:
            MEMORY[agent_name] = ""
            # Créer le fichier vide
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("")

def save_memory(agent_name):
    if agent_name not in AGENTS:
        return
    filepath = os.path.join(MEMORY_DIR, AGENTS[agent_name]["memory_file"])
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(MEMORY[agent_name])
    git_push()

# ================= GIT PUSH POUR TOUS LES FICHIERS =================
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

# ================= OUTILS DE RECHERCHE =================
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

# ================= GESTION DES CONVERSATIONS CLIENT =================
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

# ================= APPEL SIMPLE À GROQ =================
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

# ================= ORCHESTRATEUR (choix automatique des agents) =================
def orchestrate(user_text, mem_context):
    # Demander à Groq quel agent utiliser
    agent_list = "\n".join([f"- {name}: {cfg['description']}" for name, cfg in AGENTS.items()])
    prompt = f"""Tu es un aiguilleur automatique. Analyse le message utilisateur et décide quel agent doit répondre.
Agents disponibles :
{agent_list}

Règles :
- Si le message concerne une recherche, une tendance, une analyse, choisis "Market".
- Si le message demande un contenu créatif, un post Facebook, un message de prospection, choisis "Créa".
- Si le message parle d'argent, de tarifs, de devis, de comptabilité, choisis "Coco".
- Si c'est une conversation commerciale avec un prospect (qualification, vente 3D), choisis "Larry".
- Si c'est une demande administrative, de gestion, ou de supervision, choisis "NovaBot".
- Si la tâche nécessite plusieurs étapes, propose une séquence d'agents dans l'ordre, ex: Market -> Créa.

Réponds UNIQUEMENT par le nom de l'agent ou une séquence avec -> (ex: "Market" ou "Market -> Créa"). Pas de phrase supplémentaire.
Message utilisateur : {user_text}
"""
    decision = simple_groq_call([{"role": "user", "content": prompt}], max_tokens=50).strip()
    return decision

def process_admin_message(user_text):
    """Traite un message admin en mode multi-agents automatique."""
    # Mémoire de NovaBot (administrateur)
    mem_context = MEMORY.get("NovaBot", "")

    # 1. Décider quel agent utiliser
    plan = orchestrate(user_text, mem_context)
    logging.info(f"Plan d'agents: {plan}")

    # Si le plan contient "->", c'est une séquence
    if "->" in plan:
        agents_to_call = [a.strip() for a in plan.split("->")]
    else:
        agents_to_call = [plan.strip()]

    # 2. Exécuter les agents dans l'ordre
    context = user_text  # le message original
    responses = []
    for agent_name in agents_to_call:
        if agent_name not in AGENTS:
            # Agent inconnu, on utilise NovaBot par défaut
            agent_name = "NovaBot"
        # Construire les messages pour cet agent
        agent_memory = MEMORY.get(agent_name, "")
        agent_prompt = AGENTS[agent_name]["prompt"]
        messages = [
            {"role": "system", "content": agent_prompt},
        ]
        if agent_memory:
            messages.append({"role": "system", "content": f"Mémoire de {agent_name} :\n{agent_memory}"})
        # Si ce n'est pas le premier agent, on ajoute le résultat précédent
        if len(responses) > 0:
            context = f"Contexte précédent : {responses[-1]}\n\nTâche : {user_text}"
        else:
            context = user_text
        messages.append({"role": "user", "content": context})
        resp = simple_groq_call(messages)
        responses.append(resp)
        # Sauvegarder dans la mémoire de l'agent si celui-ci a utilisé [MEMO:...]
        resp = handle_memo_action(resp, agent_name)  # adapté pour agent spécifique

    # 3. Retourner la dernière réponse (ou une combinaison)
    if len(responses) == 1:
        return responses[0]
    else:
        # Assembler les résultats avec un résumé final par NovaBot
        combined = "\n\n".join([f"**{agents_to_call[i]}** : {r}" for i, r in enumerate(responses)])
        # Demander à NovaBot de faire un résumé propre
        final_prompt = f"Voici les résultats de différents agents pour la tâche '{user_text}' :\n{combined}\nRédige une réponse finale cohérente."
        return simple_groq_call([
            {"role": "system", "content": AGENTS["NovaBot"]["prompt"]},
            {"role": "user", "content": final_prompt}
        ])

def handle_memo_action(response_text, agent_name):
    """Extrait les mises à jour mémoire [MEMO:fichier] et les applique."""
    lines = response_text.split("\n")
    clean_lines = []
    for line in lines:
        if line.startswith("[MEMO:"):
            try:
                after_bracket = line[len("[MEMO:"):]
                key, value = after_bracket.split("]", 1)
                key = key.strip()
                # Ici on ignore le fichier, on met à jour la mémoire de l'agent directement
                if agent_name in MEMORY:
                    MEMORY[agent_name] += value.strip() + "\n"
                    save_memory(agent_name)
            except:
                pass
        else:
            clean_lines.append(line)
    return "\n".join(clean_lines)

# ================= MODE CLIENT (Larry) =================
def process_client_message(user_text, chat_id):
    if pending_admin_chat_id == chat_id:
        return None
    history = get_conversation_context(chat_id)
    system_prompt = AGENTS["Larry"]["prompt"]
    if history:
        system_prompt += f"\n\nHistorique de la conversation :\n{history}\n\nRéponds en tenant compte de ce contexte."
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text}
    ]
    resp = simple_groq_call(messages)
    # Sauvegarder la réponse dans la mémoire de Larry si nécessaire
    resp = handle_memo_action(resp, "Larry")
    return resp

# ================= HANDLER TELEGRAM =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if is_authorized(message):
        bot.reply_to(message, """👑 Mode Administrateur (agents disponibles : NovaBot, Market, Créa, Coco)
Parlez-moi de votre besoin, je choisirai le bon agent automatiquement.""")
    else:
        username = message.chat.username or message.chat.first_name or "vous"
        bot.reply_to(message, f"Bonjour {username}, je suis Larry, conseiller chez NovaTech-IA. Comment puis-je vous aider ?")

@bot.message_handler(commands=['mem'])
@authorized_only
def show_memory(message):
    text = "=== MÉMOIRES DES AGENTS ===\n"
    for name in AGENTS:
        text += f"\n--- {name} ---\n{MEMORY.get(name, '(vide)')}"
    bot.reply_to(message, text[:4000])

@bot.message_handler(commands=['save'])
@authorized_only
def save_memory_command(message):
    try:
        parts = message.text.split(" ", 2)
        if len(parts) < 3:
            bot.reply_to(message, "Usage : /save <agent> <texte>")
            return
        agent_name = parts[1].capitalize()
        text = parts[2]
        if agent_name not in AGENTS:
            bot.reply_to(message, f"Agents disponibles : {', '.join(AGENTS.keys())}")
            return
        MEMORY[agent_name] += text.strip() + "\n"
        save_memory(agent_name)
        bot.reply_to(message, f"✅ Ajouté à la mémoire de {agent_name}")
    except Exception as e:
        bot.reply_to(message, f"Erreur : {str(e)}")

@bot.message_handler(commands=['scrape'])
@authorized_only
def scrape_manuel(message):
    # Utilise Market pour scraper
    prompt = f"Utilise search_web pour trouver des annonces immobilières à Cotonou sur keur-immo.com/benin et donne-moi un résumé."
    response = process_admin_message(prompt)
    bot.reply_to(message, response)

# Les autres commandes (/search, /pc, /dire) restent disponibles mais non modifiées ici.

@bot.message_handler(func=lambda m: True)
def handle_all(message):
    chat_id = str(message.chat.id)
    is_admin = is_authorized(message)

    if not is_admin and pending_admin_chat_id == chat_id:
        bot.reply_to(message, "Veuillez patienter, un conseiller va vous répondre personnellement.")
        return

    if not is_admin:
        # Mode Larry
        conversations[chat_id].append(("user", message.text))
        if len(conversations[chat_id]) > 10:
            conversations[chat_id] = conversations[chat_id][-10:]
        last_activity[chat_id] = datetime.now()
        response = process_client_message(message.text, chat_id)
        if response:
            conversations[chat_id].append(("bot", response))
            # Détection résumé Larry -> admin
            if "[RESUME]" in response:
                admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
                if admin_chat_id:
                    match = re.search(r"\[RESUME\](.*?)\[FIN RESUME\]", response, re.DOTALL)
                    if match:
                        resume = match.group(1).strip()
                        bot.send_message(admin_chat_id, f"📩 Résumé prospect :\n{resume}")
                        pending_admin_chat_id = chat_id
            # Nettoyer le résumé de la réponse visible
            response = re.sub(r"\[RESUME\].*?\[FIN RESUME\]", "", response, flags=re.DOTALL).strip()
        bot.reply_to(message, response)
    else:
        # Mode admin multi-agents
        bot.send_chat_action(message.chat.id, 'typing')
        response = process_admin_message(message.text)
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
