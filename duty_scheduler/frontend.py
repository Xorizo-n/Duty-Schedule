from flask import Blueprint, current_app, render_template


frontend_bp = Blueprint("frontend", __name__)


@frontend_bp.route("/")
def index():
    return render_template("index.html", version=current_app.config["APP_VERSION"])
