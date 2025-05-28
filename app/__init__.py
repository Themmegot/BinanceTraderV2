from flask import Flask
from app.routes import bp as trading_bp
from app.logger import configure_logging
from app.config import Config
from app.extensions import celery
import os

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Setup logging
    configure_logging()

    # Register Blueprints
    app.register_blueprint(trading_bp)

    # Init Celery
    init_celery(celery, app)

    return app


def init_celery(celery, app):
    celery.conf.update(
        broker_url=os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
        result_backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
    )
    TaskBase = celery.Task

    class ContextTask(TaskBase):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)

    celery.Task = ContextTask
