import logging
import os

from flask import Flask

from .routes import bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
    app.config["DATABASE_URL"] = os.environ["DATABASE_URL"]
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.register_blueprint(bp)
    return app
