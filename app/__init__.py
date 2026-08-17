from flask import Flask, jsonify
from flask_cors import CORS
from app.config import Config, db_instance, ensure_indexes
from app.controllers import auth_bp, sync_bp, desktop_bp
from app.utils.rate_limit import limiter


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = Config.SECRET_KEY
    app.config['DB'] = db_instance.get_db()
    app.config['MAX_CONTENT_LENGTH'] = Config.MAX_CONTENT_LENGTH_MB * 1024 * 1024
    ensure_indexes(app.config['DB'])

    CORS(app, origins=Config.CORS_ORIGINS)

    limiter.init_app(app)
    limiter.enabled = Config.RATELIMIT_ENABLED

    app.register_blueprint(auth_bp, url_prefix='/v1/auth')
    app.register_blueprint(sync_bp, url_prefix='/v1/sync')
    app.register_blueprint(desktop_bp, url_prefix='/v1/desktop')

    # Uniform JSON errors — no HTML error pages, no internals in responses.
    @app.errorhandler(413)
    def payload_too_large(_e):
        return jsonify({'message': f'Request exceeds the {Config.MAX_CONTENT_LENGTH_MB}MB limit'}), 413

    @app.errorhandler(429)
    def too_many_requests(_e):
        return jsonify({'message': 'Too many requests — try again later'}), 429

    @app.errorhandler(500)
    def internal_error(_e):
        return jsonify({'message': 'An error occurred'}), 500

    def check_mongo():
        try:
            db_instance.get_db().command('ping')
            return True
        except Exception:
            return False

    @app.route('/')
    def index():
        # Landing page is opt-out: on a public host it leaks storage status.
        if not Config.SHOW_STATUS_PAGE:
            return jsonify({'message': 'Not found'}), 404

        mongo_ok = check_mongo()
        mongo_color = '#2ecc71' if mongo_ok else '#e74c3c'
        mongo_label = 'Storage operates' if mongo_ok else 'Storage unreachable'

        return f'''
        <html>
        <head>
            <title>Smart Teeth API</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    display: flex; justify-content: center; align-items: center;
                    height: 100vh; background: linear-gradient(135deg, #e0f7fa, #f0f2f5);
                }}
                .card {{
                    text-align: center; padding: 48px 56px;
                    background: white; border-radius: 16px;
                    box-shadow: 0 4px 24px rgba(0,0,0,0.08);
                }}
                h1 {{ color: #1a1a2e; font-size: 22px; font-weight: 600; margin-bottom: 16px; }}
                .status-row {{
                    display: flex; align-items: center; justify-content: center;
                    gap: 8px; font-size: 15px; color: #444;
                }}
                .dot {{
                    display: inline-block; width: 10px; height: 10px;
                    border-radius: 50%; margin-right: 4px;
                    animation: pulse 2s infinite;
                }}
                .dot-green {{ background: #2ecc71; box-shadow: 0 0 8px rgba(46,204,113,0.5); }}
                .dot-red {{ background: #e74c3c; box-shadow: 0 0 8px rgba(231,76,60,0.5); }}
                @keyframes pulse {{
                    0%, 100% {{ opacity: 1; }}
                    50% {{ opacity: 0.4; }}
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1><span class="dot dot-green"></span>Smart Teeth server is up and running</h1>
                <div class="status-row">
                    <span class="dot {"dot-green" if mongo_ok else "dot-red"}"></span>
                    <span style="color: {mongo_color}; font-weight: 500;">{mongo_label}</span>
                </div>
            </div>
        </body>
        </html>
        '''

    return app