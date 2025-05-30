# Developer Guide

This guide outlines how to work with the BinanceTrader codebase during development.

## Project Structure

- `run.py`: Entry point for the web server.
- `celery_worker.py`: Starts the Celery worker.
- `app/`: Contains all application logic and configuration.
  - `routes.py`: Flask routes, including the webhook endpoint.
  - `tasks.py`: Celery tasks for trade handling.
  - `utils.py`: Helper methods, including Binance logic.
  - `logger.py`: Logging configuration.
  - `config.py`: Environment-based configuration.
  - `extensions.py`: Shared app extensions.
  - `validators.py`: Input validation functions.

## Running Locally

```bash
docker-compose up --build
```

The app will be available at `http://localhost:5001`.

## Running in Background

```bash
docker-compose up --build -d
```

## Logs

Application logs are saved in the `logs/` directory:

- `info.log`: General logs.
- `errors.log`: Error logs.
- `transactions.csv`: Trade transactions.

You can also follow logs in real-time with:

```bash
docker-compose logs -f
```

