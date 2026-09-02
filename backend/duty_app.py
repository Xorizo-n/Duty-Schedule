from duty_scheduler import create_app, get_config, start_background_workers


app = create_app()


def main() -> None:
    config = get_config(app)
    print("=" * 60)
    print("Запуск Duty Schedule App")
    print("=" * 60)
    print(f"Часовой пояс сервера: {config.server_timezone}")
    print(f"Google Sheet URL: {config.google_sheet_url[:50]}...")
    print(f"VK уведомления: {'включены' if config.vk_bot_token and config.vk_peer_id else 'отключены'}")
    print(f"Версия: {config.app_version}")
    print("=" * 60)

    start_background_workers(app)
    app.run(debug=False, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
