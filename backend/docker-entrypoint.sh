#!/bin/sh
# Migrations run only where PTAX_RUN_MIGRATIONS=1 (the API task), under an advisory lock.
set -e
if [ "${PTAX_RUN_MIGRATIONS:-0}" = "1" ]; then
  python -m ptax.migrate
fi
exec "$@"
