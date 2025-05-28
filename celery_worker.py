from app.extensions import celery
from app import create_app

app = create_app()

if __name__ == '__main__':
    # Worker can be started with: celery -A celery_worker.celery worker --loglevel=info
    app.app_context().push()
