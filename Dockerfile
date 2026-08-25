# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# PYTHONUNBUFFERED : sans ça, la sortie est bufferisée hors terminal et les
# journaux n'apparaissent qu'avec du retard dans `make logs`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dépendances d'abord, code ensuite : une modification du code ne réinvalide
# pas la couche d'installation pip.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . /app

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Define environment variable
ENV FLASK_APP=run.py

# Run commands to start the application with Gunicorn
# gthread workers allow concurrent request handling while background threads process data
CMD ["gunicorn", "--timeout", "600", "--worker-class", "gthread", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:5000", "run:app"]
