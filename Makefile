.PHONY: selfhost selfhost-down selfhost-logs selfhost-migrate selfhost-psql selfhost-reset selfhost-status

selfhost:
	@./scripts/selfhost.sh up

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
