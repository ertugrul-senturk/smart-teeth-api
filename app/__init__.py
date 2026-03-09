from flask import Flask
from flask_cors import CORS
from app.config import Config, db_instance
from app.controllers import auth_bp, sync_bp


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = Config.SECRET_KEY
    app.config['DB'] = db_instance.get_db()

    CORS(app)

    app.register_blueprint(auth_bp, url_prefix='/v1/auth')
    app.register_blueprint(sync_bp, url_prefix='/v1/sync')

    return app