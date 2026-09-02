from flask import Blueprint, current_app, render_template


views_bp = Blueprint("views", __name__)


@views_bp.route("/")
def index():
    return render_template("index.html", version=current_app.config["APP_VERSION"])
