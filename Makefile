.PHONY: backend-test frontend-build root-test test release-check release-set-version selfhost selfhost-deploy-backend selfhost-deploy-frontend selfhost-down selfhost-logs selfhost-migrate selfhost-psql selfhost-reset selfhost-restart-postgres selfhost-status

root-test:
	@python3 -m unittest discover -s tests -v

backend-test:
	@cd backend && uv run python -m unittest discover -v

frontend-build:
	@cd frontend && pnpm build

test: root-test backend-test frontend-build

release-check:
	@python3 scripts/release_version.py check

release-set-version:
	@test -n "$(VERSION)" || (echo "Usage: make release-set-version VERSION=0.1.1" >&2; exit 2)
	@python3 scripts/release_version.py set "$(VERSION)"

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
