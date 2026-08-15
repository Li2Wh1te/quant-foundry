.PHONY: backend-test frontend-build test selfhost selfhost-deploy-backend selfhost-deploy-frontend selfhost-down selfhost-logs selfhost-migrate selfhost-psql selfhost-reset selfhost-restart-postgres selfhost-status

backend-test:
	@cd backend && uv run python -m unittest discover -v

frontend-build:
	@cd frontend && pnpm build

test: backend-test frontend-build

selfhost:
	@./scripts/selfhost.sh up

selfhost-deploy-frontend:
	@./scripts/selfhost.sh deploy-frontend

selfhost-deploy-backend:
	@./scripts/selfhost.sh deploy-backend

selfhost-restart-postgres:
	@./scripts/selfhost.sh restart-postgres

selfhost-down:
	@./scripts/selfhost.sh down

selfhost-logs:
	@./scripts/selfhost.sh logs

selfhost-migrate:
	@./scripts/selfhost.sh migrate

selfhost-psql:
	@./scripts/selfhost.sh psql

selfhost-reset:
	@./scripts/selfhost.sh reset

selfhost-status:
	@./scripts/selfhost.sh status
