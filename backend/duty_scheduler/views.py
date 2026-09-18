from flask import Blueprint, current_app, render_template


views_bp = Blueprint("views", __name__)


@views_bp.route("/")
def index():
    return render_template("index.html", version=current_app.config["APP_VERSION"])


@views_bp.route("/settings")
def settings():
    # Страница отдаётся без пароля: всё, что она показывает, приходит из
    # /api/settings, а там вход уже проверяется.
    return render_template("settings.html", version=current_app.config["APP_VERSION"])
