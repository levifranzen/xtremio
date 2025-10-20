# Base image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy application code
COPY api/ /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose Flask's default port
EXPOSE 5000

# Run the Flask app
CMD ["python", "index.py"]
