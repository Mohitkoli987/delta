#!/bin/bash

# Render startup script for Auto Trading Bot
echo "Starting Auto Trading Bot on Render..."

# Set Python path
export PYTHONPATH=/app

# Initialize database
echo "Initializing MySQL database..."
python app.py init_db

# Start the application with Gunicorn
echo "Starting Flask app with Gunicorn..."
exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --timeout 120 app:app
