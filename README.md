# Polymarket Screener

Django + PostgreSQL backend and React + TypeScript frontend scaffold.

## Local Development

```bash
cp .env.example .env
docker compose up --build
```

Services:

- Frontend: `docker compose port frontend 5173`
- Backend: `docker compose port backend 8000`
- Postgres: `docker compose port db 5432`
- ClickHouse: `docker compose port clickhouse 8123`

All host ports default to `0`, so Docker picks a free external port for each
service. Set `POSTGRES_HOST_PORT`, `CLICKHOUSE_HOST_PORT`,
`BACKEND_HOST_PORT`, or `FRONTEND_HOST_PORT` in `.env` if you need fixed host
ports locally.

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
