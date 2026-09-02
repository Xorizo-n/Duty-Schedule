from flask import Blueprint, current_app, jsonify

from .schedule_service import ScheduleService


api_bp = Blueprint("api", __name__)


def get_schedule_service() -> ScheduleService:
    return current_app.extensions["schedule_service"]


@api_bp.route("/api/data")
def api_data():
    service = get_schedule_service()
    current_dt = service.get_current_datetime()
    return jsonify(
        {
            "success": True,
            "data": service.build_api_payload(),
            "timestamp": current_dt.timestamp(),
        }
    )


@api_bp.route("/api/health")
@api_bp.route("/health")
def health():
    service = get_schedule_service()
    return jsonify(service.build_health_payload())


@api_bp.route("/version")
def version():
    current_dt = get_schedule_service().get_current_datetime()
    return jsonify(
        {
            "version": current_app.config["APP_VERSION"],
            "timestamp": current_dt.timestamp(),
        }
    )
