from flask import Blueprint, abort, jsonify

from .repository import get_user

bp = Blueprint("api", __name__)


@bp.get("/health")
def health():
    return jsonify(status="ok")


@bp.get("/users/<int:user_id>")
def user_detail(user_id: int):
    user = get_user(user_id)
    if user is None:
        abort(404)
    return jsonify(
        id=user["id"],
        name=user["name"],
        email=user["email"],  # BUG: column was renamed to `mail`
    )
