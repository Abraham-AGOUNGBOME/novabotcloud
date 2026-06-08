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

# ================= MÉMOIRE PARTAGÉE ET MULTI-AGENTS =================
MEMORY_DIR = "memory"
SHARED_FILES = ["cibles.md", "tarifs.md", "apprentissages.md"]
AGENTS = {
    "NovaBot": {
        "prompt": """Tu es NovaBot, l'assistant administratif central de NovaTech-IA, Cotonou, Bénin.
Tu supervises les autres agents (Market, Créa, Coco, Larry) et tu prends les décisions finales.
Tu as accès à toutes les mémoires. Sois concis et orienté action.
Quand tu reçois les résultats d'autres agents, vérifie leur cohérence avant de répondre.
Tu dois toujours utiliser les tarifs officiels fournis dans le contexte partagé, sans les modifier.""",
        "memory_file": "novabot_memory.md",
        "description": "Assistant principal, gestion administrative, supervision"
    },
    "Market": {
        "prompt": """Tu es Market, l'agent d'analyse de marché et de tendances.
Tu cherches sur le web, analyses les données, et fournis des rapports avec sources.
Utilise l'outil search_web quand nécessaire. Sois précis et chiffré.
Respecte scrupuleusement le contexte de NovaTech-IA : localisation Cotonou, Bénin, secteur des visites 3D immobilières.
Ne donne jamais de tarif, sauf si tu cites exactement le bloc officiel fourni dans le contexte partagé.""",
        "memory_file": "market_memory.md",
        "description": "Recherche de tendances, analyse de marché, veille concurrentielle"
    },
    "Créa": {
        "prompt": """Tu es Créa, l'agent créatif de NovaTech-IA, Cotonou, Bénin.
Tu rédiges des posts Facebook, messages WhatsApp, accroches marketing.

RÈGLES ABSOLUES :
- **Les seuls tarifs autorisés sont ceux du bloc officiel ci-dessous.** Tu dois les reproduire EXACTEMENT, sans ajout ni modification.
- **Tu n'as pas le droit de créer un nouveau tarif**, une promotion, un abonnement, ou un forfait.
- Si on te demande un devis ou un prix, réponds EXACTEMENT :
   "Voici nos tarifs officiels :
   - Scan 3D standard : 75 000 FCFA
   - Scan 3D premium : 120 000 FCFA
   - Hébergement mensuel : 5 000 FCFA/mois"
- Tu ne mentionnes JAMAIS un autre pays que le Bénin.
- Tu n'inventes jamais de statistiques ou de chiffres.
- Si tu ne disposes pas d'une information, réponds "Je ne dispose pas de cette information" plutôt que d'inventer.

EXEMPLE INTERDIT (ne fais jamais cela) :
❌ "Abonnement mensuel à 150 000 FCFA" → INVENTÉ, INTERDIT.
❌ "Tarif spécial de lancement" → INVENTÉ, INTERDIT.

EXEMPLE CORRECT :
✅ "Notre visite 3D premium coûte 120 000 FCFA."

Reste punchy, local, mais toujours factuel.
""",
        "memory_file": "crea_memory.md",
        "description": "Création de contenu, rédaction marketing, posts réseaux sociaux"
    },
    "Coco": {
        "prompt": """Tu es Coco, le comptable de NovaTech-IA.
Tu fournis des informations financières exactes, basées UNIQUEMENT sur les données de l'entreprise.

TARIFS OFFICIELS (seuls chiffres que tu peux utiliser) :
- Prix de vente scan 3D standard : 75 000 FCFA
- Prix de vente scan 3D premium : 120 000 FCFA
- Hébergement mensuel : 5 000 FCFA/mois
- Coût de revient estimé d'un scan standard : 25 000 FCFA (temps, déplacement, logiciel)

INTERDICTIONS :
- **Ne jamais inventer de nouveau tarif**, forfait, abonnement, remise.
- **Ne jamais convertir en euros**, dollars, ou autre devise.
- Si une information n'est pas dans la liste ci-dessus, réponds : "Je ne dispose pas de cette information financière."

FORMAT OBLIGATOIRE POUR UNE DEMANDE DE PRIX :
"Le prix de vente du scan 3D standard est de 75 000 FCFA, avec un coût de revient d'environ 25 000 FCFA."
Ne dévie jamais de ce format.
""",
        "memory_file": "coco_memory.md",
        "description": "Comptabilité, tarifs, devis, suivi financier"
    },
    "Larry": {
        "prompt": """Tu es Larry, l'agent commercial pour les visites 3D.
Tu parles aux clients potentiels, tu les qualifies, tu ne forces jamais.
Ne mentionne pas les autres agents. Reste chaleureux et professionnel.
Service : visites virtuelles 3D pour immobilier à Cotonou (Fidjrossè, Haie Vive, etc.).
Si le client demande un prix, utilise UNIQUEMENT les tarifs officiels présents dans le contexte partagé.
Ne divague jamais sur d'autres services.
""",
        "memory_file": "larry_memory.md",
        "description": "Vente et qualification de prospects pour visites 3D"
    }
}

MEMORY = {}          # mémoire individuelle des agents
SHARED_MEMORY = {}   # mémoire commune (cibles, tarifs, apprentissages)

def load_all_memory():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    # Mémoires individuelles
    for agent_name, cfg in AGENTS.items():
        filepath = os.path.join(MEMORY_DIR, cfg["memory_file"])
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                MEMORY[agent_name] = f.read()
        except FileNotFoundError:
            MEMORY[agent_name] = ""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("")
    # Mémoire partagée
    for fname in SHARED_FILES:
        filepath = os.path.join(MEMORY_DIR, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                SHARED_MEMORY[fname.replace(".md", "")] = f.read()
        except FileNotFoundError:
            SHARED_MEMORY[fname.replace(".md", "")] = ""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("")

def save_memory(agent_name):
    if agent_name not in AGENTS:
        return
    filepath = os.path.join(MEMORY_DIR, AGENTS[agent_name]["memory_file"])
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(MEMORY[agent_name])
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

# ================= HARNAIS DE VÉRIFICATION =================
def verify_output(agent_name, output):
    """Vérifie la cohérence de la sortie (pays, tarifs inventés, etc.)."""
    # Vérification des prix interdits
    tarifs_autorises = ["75 000", "120 000", "5 000", "25 000"]  # en FCFA
    # Capturer tous les montants en FCFA
    mots_prix = re.findall(r'\d[\d\s]*\d?\s?FCFA', output)
    for prix in mots_prix:
        chiffre = re.sub(r'[^\d]', '', prix)
        if chiffre not in [t.replace(' ', '') for t in tarifs_autorises]:
            return f"❌ Prix inventé détecté ({prix}). Utilisez uniquement les tarifs officiels."

    # Vérification pays
    if "Côte d'Ivoire" in output or "Côte d’Ivoire" in output or "côte d'ivoire" in output.lower():
        return "❌ Mention d'un autre pays que le Bénin. Correction obligatoire."
    if "euro" in output.lower() and "FCFA" not in output:
        return "❌ Devise incorrecte. Tous les montants doivent être en FCFA."

    # Vérification générique de cohérence via Groq
    prompt = f"""Tu es un vérificateur qualité pour NovaTech-IA (Cotonou, Bénin).
Analyse cette sortie produite par l'agent {agent_name}.
Contexte : visites 3D immobilières, tarifs stricts (75k, 120k, 5k FCFA).
Si tout est correct, réponds "OK".
Si un problème est détecté (autre pays, prix faux, service hors sujet), réponds "PROBLÈME : <description>".
Sortie : {output[:1500]}"""
    resp = simple_groq_call([{"role": "user", "content": prompt}], max_tokens=100)
    if "OK" in resp:
        return output
    else:
        # Force une correction
        correction_prompt = f"""Corrige la sortie suivante selon cette remarque : {resp}
Respecte scrupuleusement les tarifs officiels (75k, 120k, 5k FCFA) et le pays (Bénin).
Sortie rejetée : {output[:1000]}
Nouvelle version :"""
        corrected = simple_groq_call([{"role": "user", "content": correction_prompt}], max_tokens=1000)
        return corrected

def validate_plan(plan, user_text):
    """Valide que la séquence d'agents est pertinente."""
    prompt = f"""Superviseur, valide ce plan d'action pour : "{user_text}"
Plan proposé : {plan}
Réponds "OK" si adapté, sinon propose une meilleure séquence (ex: Market -> Créa)."""
    resp = simple_groq_call([{"role": "user", "content": prompt}], max_tokens=50)
    return "OK" in resp

def orchestrate(user_text, shared_context):
    """Choisit le(s) agent(s) le(s) plus adapté(s)."""
    agent_list = "\n".join([f"- {name}: {cfg['description']}" for name, cfg in AGENTS.items()])
    prompt = f"""Aiguilleur automatique. Analyse le message utilisateur et décide quel agent doit répondre.
Agents disponibles :
{agent_list}

Règles :
- Recherche, tendances, analyse → Market
- Contenu créatif, post Facebook, message prospection → Créa
- Argent, tarifs, devis, comptabilité → Coco (PRIORITAIRE pour toute question de prix)
- Conversation commerciale avec un prospect → Larry
- Supervision, coordination, demande administrative → NovaBot
- Si la tâche nécessite plusieurs étapes, donne une séquence avec -> (ex: Market -> Créa).
- Ne jamais envoyer une question purement tarifaire à Créa.

Réponds UNIQUEMENT par le nom de l'agent ou une séquence (ex: "Market" ou "Coco").
Message utilisateur : {user_text}"""
    return simple_groq_call([{"role": "user", "content": prompt}], max_tokens=50).strip()

# ================= TRAITEMENT ADMIN MULTI-AGENTS =================
def process_admin_message(user_text):
    shared_context = ""
    for key, content in SHARED_MEMORY.items():
        if content.strip():
            shared_context += f"=== {key}.md ===\n{content}\n\n"

    # Ajouter instruction spéciale pour les tarifs
    shared_context += "\n**RAPPEL ABSOLU : Les seuls tarifs autorisés sont les suivants. Vous devez les reproduire à l'identique :**\n"
    shared_context += "- Scan 3D standard : 75 000 FCFA\n- Scan 3D premium : 120 000 FCFA\n- Hébergement mensuel : 5 000 FCFA/mois\n"

    plan = orchestrate(user_text, shared_context)
    if not validate_plan(plan, user_text):
        return "Le plan proposé n'a pas été validé par le superviseur. Veuillez reformuler votre demande."

    agents_to_call = [a.strip() for a in plan.split("->")] if "->" in plan else [plan.strip()]
    context = user_text
    responses = []

    for agent_name in agents_to_call:
        if agent_name not in AGENTS:
            agent_name = "NovaBot"

        agent_prompt = AGENTS[agent_name]["prompt"] + f"\n\nContexte partagé de NovaTech-IA :\n{shared_context}"
        agent_memory = MEMORY.get(agent_name, "")
        messages = [{"role": "system", "content": agent_prompt}]
        if agent_memory:
            messages.append({"role": "system", "content": f"Mémoire personnelle de {agent_name} :\n{agent_memory}"})

        if len(responses) > 0:
            context = f"Contexte précédent : {responses[-1]}\n\nTâche : {user_text}"
        else:
            context = user_text
        messages.append({"role": "user", "content": context})

        resp = simple_groq_call(messages)
        resp = verify_output(agent_name, resp)
        responses.append(resp)
        resp = handle_memo_action(resp, agent_name)

    if len(responses) == 1:
        return responses[0]
    else:
        combined = "\n\n".join([f"**{agents_to_call[i]}** : {r}" for i, r in enumerate(responses)])
        final_prompt = f"Voici les résultats de différents agents pour la tâche '{user_text}' :\n{combined}\nRédige une réponse finale cohérente."
        final_resp = simple_groq_call([
            {"role": "system", "content": AGENTS["NovaBot"]["prompt"]},
            {"role": "user", "content": final_prompt}
        ])
        return verify_output("Orchestrateur", final_resp)

def handle_memo_action(response_text, agent_name):
    lines = response_text.split("\n")
    clean_lines = []
    for line in lines:
        if line.startswith("[MEMO:"):
            try:
                after_bracket = line[len("[MEMO:"):]
                key, value = after_bracket.split("]", 1)
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
    resp = handle_memo_action(resp, "Larry")
    return resp

# ================= COMMANDES TELEGRAM =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if is_authorized(message):
        bot.reply_to(message, """👑 Mode Administrateur (agents : NovaBot, Market, Créa, Coco)
Décrivez votre besoin, le bon agent vous répondra automatiquement.""")
    else:
        username = message.chat.username or message.chat.first_name or "vous"
        bot.reply_to(message, f"Bonjour {username}, je suis Larry, conseiller chez NovaTech-IA. Comment puis-je vous aider ?")

@bot.message_handler(commands=['mem'])
@authorized_only
def show_memory(message):
    text = "=== MÉMOIRES DES AGENTS ===\n"
    for name in AGENTS:
        text += f"\n--- {name} ---\n{MEMORY.get(name, '(vide)')}"
    text += "\n\n=== MÉMOIRE PARTAGÉE ===\n"
    for key, content in SHARED_MEMORY.items():
        text += f"\n--- {key}.md ---\n{content if content else '(vide)'}"
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
    prompt = "Utilise search_web pour trouver des annonces immobilières récentes à Cotonou et donne un résumé avec les sources."
    response = process_admin_message(prompt)
    bot.reply_to(message, response)

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
    if isinstance(results, list):
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
