if __name__ == '__main__':
    git_setup()
    git_pull()
    load_memory()

    # Démarrer Flask (obligatoire pour le webhook)
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Configurer le webhook Telegram
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    if webhook_url:
        bot.remove_webhook()  # Nettoie tout ancien webhook
        time.sleep(1)
        bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook configuré sur {webhook_url}")
    else:
        logging.warning("WEBHOOK_URL non défini, utilisation du polling (moins fiable)")
        bot.infinity_polling()

    # Flask tourne indéfiniment (le webhook utilise la route '/')
    while True:
        time.sleep(60)
