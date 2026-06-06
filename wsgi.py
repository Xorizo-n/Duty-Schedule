from duty_scheduler import create_app, start_background_workers


app = create_app()
start_background_workers(app)
