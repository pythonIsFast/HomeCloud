"""HomeCloud application factory.

Usage:
    development:  flask --app app run --debug --port 6002
    production:   gunicorn "app:create_app()" --bind 127.0.0.1:6002
"""

import os

from flask import Flask

from . import audit
from . import config as app_config
from . import db
from . import security


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
    security.register(app)
    # "flask --app app prune-audit"
    audit.register(app)
    # "flask --app app compute-build-image"
    register_cli(app)

    # ---- Blueprints -------------------------------------------------------
    # auth: registration, login/logout, JWT, API keys
    from .auth import bp as auth_bp

    app.register_blueprint(auth_bp)

    # core: dashboard page, resource registry API, admin quota endpoints
    from .core import bp as core_bp

    app.register_blueprint(core_bp)

    # services/: one blueprint per service.
    from .services.compute import bp as compute_bp

    app.register_blueprint(compute_bp)

    return app


def register_cli(app):
    """Setup commands that are neither database nor audit related."""
    import click

    @app.cli.command("compute-build-image")
    @click.option("--size-mb", default=1024, show_default=True,
                  help="Size of the base image; each VM grows its own copy.")
    def compute_build_image(size_mb):
        """Build the writable ext4 base image from the Firecracker CI squashfs.

        Run once after downloading the images. Needs no root: mke2fs builds the
        filesystem from a directory tree without mounting anything.
        """
        # Imported here so the web app does not pull in the VM modules on every
        # start -- they are only needed for setup and for the worker.
        from .vmm import images

        images.build_base_image(
            app.config["VM_BASE_SQUASHFS"],
            app.config["VM_BASE_ROOTFS"],
            size_mb=size_mb,
            log=click.echo,
        )

    @app.cli.command("show-config")
    def show_config():
        """Print the resolved paths, to check a deployment without guessing."""
        for key in ("DATABASE", "FIRECRACKER_BIN", "VM_KERNEL", "VM_BASE_ROOTFS",
                    "VM_BASE_SQUASHFS", "VM_DIR", "VM_SUBNET_PREFIX"):
            value = app.config[key]
            marker = ""
            if key not in ("VM_SUBNET_PREFIX",) and not os.path.exists(str(value)):
                marker = "   <-- missing"
            click.echo(f"{key:18} {value}{marker}")
