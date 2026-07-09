# Polymarket Screener

Django + PostgreSQL backend and React + TypeScript frontend scaffold.

## Local Development

```bash
cp .env.example .env
docker compose up --build
```

Services:

- Backend: http://localhost:8000
- Frontend: http://localhost:5173
- Postgres: localhost:5433

## Backend Checks

```bash
python -m pip install -r backend/requirements-dev.txt
python backend/manage.py migrate
ruff check backend
mypy backend
pytest backend
```

## Frontend Checks

```bash
cd frontend
npm install
npm run typecheck
npm run build
```
