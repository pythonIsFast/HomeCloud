"""Service blueprints live here (compute, storage, database, ...).

Each service gets its own subpackage with a Blueprint, and stores its objects
as rows in the shared "resources" table via app/core/resources.py -- it must
NOT create tables of its own. Register the blueprint in app/__init__.py.
"""
