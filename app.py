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

# ================= PROMPT AMÉLIORÉ =================
SYSTEM_PROMPT = """Tu es NovaBot, l'agent commercial IA de NovaTech-IA, basé à Cotonou (Bénin).
Ton rôle : aider à vendre des visites virtuelles 3D, des bots Telegram et de l'automatisation IA aux PME béninoises.
Tu es proactif, concis et toujours orienté vers l'action commerciale.

**Règles de comportement :**
1. Quand l'utilisateur demande un message de prospection, tu génères un message prêt à envoyer, adapté au contexte local (français du Bénin, formules de politesse, référence aux quartiers).
2. Tu t'appuies systématiquement sur les fichiers mémoire : cibles.md, tarifs.md, apprentissages.md.
3. Si une information clé n'est pas dans la mémoire, tu le signales et proposes de l'ajouter via /save.
4. Après chaque échange, tu proposes une action concrète : envoi d'un message, ajout d'une cible, relance d'un prospect.
5. Tu analyses les opportunités : si un nouveau quartier ou type d'établissement est mentionné, tu suggères de l'ajouter aux cibles.

**Format de réponse :**
- Toujours signer par "— NovaBot"
- Si tu sauvegardes automatiquement une info, utilise la ligne [MEMO:fichier] contenu.
- Sinon, termine par une question ouverte pour engager la suite.

**Connaissance du marché béninois :**
- Quartiers porteurs : Fidjrossè, Cadjehoun, Haie Vive, Ganhi, Zongo.
- Budgets typiques : 75 000 - 120 000 FCFA pour un scan 3D.
- Clients types : résidences meublées, hôtels boutique, agences immobilières.
- Concurrence faible, argument principal : innovation et modernité.
"""

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

# ================= SCRAPING =================
def scrape_annonces():
    """Scrape Keur-immo Bénin et retourne les annonces filtrées par quartier."""
    try:
        url = "https://keur-immo.com/benin"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Sélecteurs à adapter si le site change (à vérifier une fois)
        annonces = soup.select(".item-listing")  # ou ".listing-item"
        nouvelles = []
        quartiers_cibles = ["fidjrossè", "haie vive", "cadjèhoun", "akpakpa", "ganhi", "zongo", "calavi"]
        
        for annonce in annonces[:10]:  # Limiter à 10 annonces pour la performance
            titre_elem = annonce.select_one("h2") or annonce.select_one(".titre")
            prix_elem = annonce.select_one(".price") or annonce.select_one(".prix")
            lien_elem = annonce.select_one("a")
            localisation_elem = annonce.select_one(".location") or annonce.select_one(".ville")
            
            titre = titre_elem.text.strip() if titre_elem else "Sans titre"
            prix = prix_elem.text.strip() if prix_elem else "N/C"
            lien = lien_elem["href"] if lien_elem and lien_elem.get("href") else ""
            localisation = localisation_elem.text.strip().lower() if localisation_elem else ""
            
            # Filtre par quartier
            if any(q in localisation for q in quartiers_cibles):
                nouvelles.append(f"🏠 {titre}\n💰 {prix}\n📍 {localisation.title()}\n🔗 {lien}\n")
        
        return "\n".join(nouvelles) if nouvelles else "Aucune annonce pertinente trouvée aujourd'hui."
    except Exception as e:
        return f"Erreur de scraping : {str(e)}"

def job_quotidien():
    chat_id = os.environ.get("ADMIN_CHAT_ID")
    if not chat_id:
        logging.warning("ADMIN_CHAT_ID non défini, impossible d'envoyer le rapport.")
        return
    rapport = scrape_annonces()
    bot.send_message(chat_id, f"📊 Rapport quotidien des annonces :\n\n{rapport}")

# Planification : tous les jours à 7h00 UTC+1 (heure de Cotonou)
scheduler.add_job(job_quotidien, 'cron', hour=7, minute=0, timezone='Africa/Porto-Novo')

# ================= COMMANDES TELEGRAM =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, """Bonjour, je suis NovaBot Cloud, toujours à votre service.

Commandes :
/mem - voir la mémoire
/save <fichier> <texte> - sauvegarder une info
/pc - commandes PC
/scrape - lancer un scraping manuel (test)

Posez-moi directement une question.""")

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

@bot.message_handler(commands=['save'])
def save_memory_command(message):
    try:
        parts = message.text.split(" ", 2)
        if len(parts) < 3:
            bot.reply_to(message, "Usage : /save <fichier> <texte>\nExemple : /save apprentissages Hôtel Le Nid intéressé")
            return
        key = parts[1].lower()
        text = parts[2]
        if key not in FILES:
            bot.reply_to(message, f"Fichier inconnu. Choisis parmi : {', '.join(FILES.keys())}")
            return
        MEMORY[key] += text.strip() + "\n"
        save_memory(key)
        bot.reply_to(message, f"✅ Ajouté à {key}.md")
    except Exception as e:
        bot.reply_to(message, f"Erreur : {str(e)}")

@bot.message_handler(commands=['scrape'])
def scrape_manuel(message):
    bot.send_chat_action(message.chat.id, 'typing')
    rapport = scrape_annonces()
    bot.reply_to(message, rapport)

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
    git_setup()
    git_pull()
    load_memory()

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    bot.infinity_polling()
