#!/usr/bin/env bash
# Shared paths for the isolated development database; never production settings.
FIFO_REPO_DIR="${FIFO_REPO_DIR:-/workspace/FiFO-move}"
FIFO_RUNTIME_DIR="${FIFO_RUNTIME_DIR:-/workspace/.fifo-env}"
FIFO_ODOO_DIR="${FIFO_ODOO_DIR:-/workspace/odoo-19}"
FIFO_PG_BIN="$FIFO_RUNTIME_DIR/sysroot/usr/lib/postgresql/17/bin"
FIFO_PYTHON="$FIFO_RUNTIME_DIR/venv/bin/python"
FIFO_DATABASE=fifo_financial_tests

fifo_start_database() {
    mkdir -p "$FIFO_RUNTIME_DIR/socket" "$FIFO_RUNTIME_DIR/odoo-data"
    if ! "$FIFO_PG_BIN/pg_ctl" -D "$FIFO_RUNTIME_DIR/pgdata" status >/dev/null 2>&1; then
        "$FIFO_PG_BIN/pg_ctl" -D "$FIFO_RUNTIME_DIR/pgdata" -l "$FIFO_RUNTIME_DIR/postgres.log" \
            -o "-k $FIFO_RUNTIME_DIR/socket -p 55432 -c listen_addresses=''" start
    fi
    "$FIFO_PG_BIN/pg_isready" -h "$FIFO_RUNTIME_DIR/socket" -p 55432
}

fifo_odoo() {
    "$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" \
        -d "$FIFO_DATABASE" --db_host="$FIFO_RUNTIME_DIR/socket" --db_port=55432 --db_user=agent \
        --addons-path="$FIFO_ODOO_DIR/addons,$FIFO_REPO_DIR" \
        --data-dir="$FIFO_RUNTIME_DIR/odoo-data" --http-interface=127.0.0.1 "$@"
}
