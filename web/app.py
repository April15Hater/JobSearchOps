"""
web/app.py — Local Flask dashboard (127.0.0.1:5001). No auth needed.
"""
import os
from flask import Flask
from web.routes import register_routes

_templates = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
app = Flask(__name__, template_folder=_templates)
app.secret_key = "jobsearch-local-only-no-auth-needed"

register_routes(app)


def run():
    app.run(host="0.0.0.0", port=5001, debug=False)
