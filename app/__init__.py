"""HomeCloud application factory.

Usage:
    development:  flask --app app run --debug --port 6002
    production:   gunicorn "app:create_app()" --bind 127.0.0.1:6002
"""

import os

from flask import Flask

from . import config as app_config
from . import db


def create_app(test_config=None):
    """Create and configure the HomeCloud Flask application."""
    # instance_relative_config puts the instance folder next to the package
    # (project_root/instance) -- that is where the SQLite file lives.
    app = Flask(__name__, instance_relative_config=True)
    # The display name lives in config["APP_NAME"]; Flask.name is read-only.

    # The instance folder is not created by Flask automatically.
    os.makedirs(app.instance_path, exist_ok=True)

    if test_config is None:
        app.config.from_mapping(app_config.build_config(app.instance_path))
    else:
        app.config.from_mapping(test_config)

    # Database: teardown handler + "flask --app app init-db" CLI command.
    db.register(app)

    # ---- Blueprints -------------------------------------------------------
    # auth: registration, login/logout, JWT, API keys
    from .auth import bp as auth_bp

    app.register_blueprint(auth_bp)

    # core: dashboard page and the service-agnostic resource registry API
    from .core import bp as core_bp

    app.register_blueprint(core_bp)

    # services/: one blueprint per service will be added here later, e.g.
    #   from .services.compute import bp as compute_bp
    #   app.register_blueprint(compute_bp)

    return app
