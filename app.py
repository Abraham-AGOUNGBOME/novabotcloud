import os
import requests
import telebot
from flask import Flask
import threading
import subprocess
import time

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
GIT_TOKEN = os.environ.get("GIT_TOKEN")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ================= MÉMOIRE PERSISTANTE =================
MEMORY_DIR = "memory"
FILES = {
    "cibles": "cibles.md",
    "tarifs": "tarifs.md",
    "apprentissages": "apprentissages.md"
}
MEMORY = {}

def git_setup():
    """Configure git avec le token pour les pushs automatiques."""
    repo_url = f"https://{GIT_TOKEN}@github.com/Abraham-AGOUNGBOME/novabotcloud.git"
    subprocess.run(["git", "remote", "set-url", "origin", repo_url], capture_output=True)
    subprocess.run(["git", "config", "user.email", "novabot@novatech.bj"], capture_output=True)
    subprocess.run(["git", "config", "user.name", "NovaBot"], capture_output=True)

def git_pull():
    """Récupère les derniers changements depuis GitHub."""
    subprocess.run(["git", "pull", "origin", "main"], capture_output=True)

def git_push():
    """Commit et push les modifications des fichiers mémoire."""
    # Ajouter seulement les fichiers du dossier memory
    subprocess.run(["git", "add", f"{MEMORY_DIR}/*.md"], capture_output=True)
    # Commit avec message horodaté
    commit_msg = f"Memory update {time.strftime('%Y-%m-%d %H:%M:%S')}"
    subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True)
    # Push
    subprocess.run(["git", "push", "origin", "main"], capture_output=True)

def load_memory():
    """Charge les fichiers .md depuis le dossier memory."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    for key, filename in FILES.items():
        filepath = os.path.join(MEMORY_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                MEMORY[key] = f.read()
        except FileNotFoundError:
            MEMORY[key] = ""

def save_memory(key):
    """Sauvegarde un fichier mémoire localement et pousse vers GitHub."""
    filepath = os.path.join(MEMORY_DIR, FILES[key])
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(MEMORY[key])
    # Pousser sur GitHub
    git_push()

# ================= PROMPT SYSTÈME =================
SYSTEM_PROMPT = """Tu es NovaBot, un agent IA de NovaTech-IA basé à Cotonou, Bénin.
Tu aides à analyser des niches de marché, trouver des prospects, rédiger des messages de prospection et des posts Facebook.
Tu as accès à une mémoire persistante :
- cibles.md : cibles prioritaires
- tarifs.md : tarifs
- apprentissages.md : ce que tu as appris

Quand une information importante doit être sauvegardée, termine ton message par :
[MEMO:nom_du_fichier] contenu à ajouter
Exemple : [MEMO:apprentissages] Fidjrossè montre un intérêt pour les visites 3D

Commandes disponibles pour l'utilisateur :
/mem - affiche l'état actuel de la mémoire
/pc - liste les commandes PC
Sois concis, professionnel, adapté au contexte béninois."""

# ================= DEEPSEEK =================
def call_deepseek(messages):
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1000
    }
    try:
        resp = requests.post(DEEPSEEK_URL, headers=headers, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Erreur API : {str(e)}"

def process_message(user_text):
    mem_context = ""
    for key, content in MEMORY.items():
        if content.strip():
            mem_context += f"=== {key}.md ===\n{content}\n\n"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Mémoire actuelle :\n{mem_context}" if mem_context else "Aucune mémoire."},
        {"role": "user", "content": user_text}
    ]
    return call_deepseek(messages)

def handle_memo_action(response_text):
    """Détecte les actions mémoire et les exécute."""
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

# ================= COMMANDES TELEGRAM =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Bonjour, je suis NovaBot Cloud, toujours à votre service.\nCommandes :\n/mem - voir la mémoire\n/pc - commandes PC\nPosez-moi directement une question.")

@bot.message_handler(commands=['mem'])
def show_memory(message):
    text = "=== MÉMOIRE ACTUELLE ===\n"
    for key, content in MEMORY.items():
        text += f"\n--- {key}.md ---\n{content if content else '(vide)'}"
    if len(text) > 4000:
        text = text[:4000] + "\n... (tronqué)"
    bot.reply_to(message, text)

@bot.message_handler(commands=['pc'])
def pc_commands(message):
    bot.reply_to(message, "Commandes PC (à connecter) : /eteindre, /redemarrer, /ram, /screenshot")

@bot.message_handler(func=lambda m: True)
def handle_all(message):
    bot.send_chat_action(message.chat.id, 'typing')
    response = process_message(message.text)
    response = handle_memo_action(response)
    bot.reply_to(message, response)

# ================= SERVEUR FLASK (health check) =================
@app.route('/')
def health():
    return 'Bot is running'

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# ================= LANCEMENT =================
if __name__ == '__main__':
    # Configuration git
    git_setup()
    # Récupérer la dernière version de la mémoire
    git_pull()
    # Charger la mémoire
    load_memory()

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    bot.infinity_polling()
