FROM python:3.11-slim

# Create a non-root user
RUN useradd --create-home appuser

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY run.py .
COPY celery_worker.py .
COPY app/ app/

# Switch to non-root user
USER appuser

# Run the Flask app
CMD ["python", "run.py"]
