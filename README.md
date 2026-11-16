# OmniFit Sync: The Universal Fitness & IoT Health Data Harmonizer

Full-stack web platform that ingests fragmented fitness and biometric telemetry (Garmin .FIT, Apple Health .XML/.ZIP, spatial .GPX, and manual logs), normalizes records into a unified relational PostgreSQL schema, and renders unified insights for Athletes and Coaches.

## Quick Start
- Backend Unit Tests: Activate backend\.venv\Scripts\Activate.ps1, set $env:PYTHONPATH=".", and run pytest -v
- Local Containers: Run docker compose up --build and open http://localhost:8000/docs
