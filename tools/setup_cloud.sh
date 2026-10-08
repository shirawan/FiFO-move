#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime.sh"
# Each cloud task is already isolated. Use the existing repository, no worktree.
FIFO_ODOO_COMMIT=9ec2b55d3fa343e6547a9db54d5f3f5c776c535d
python3 -c 'import sys; assert sys.version_info[:2] == (3, 12), "This setup is tested with Python 3.12"'
test -r /usr/share/keyrings/debian-archive-keyring.gpg
mkdir -p "$FIFO_RUNTIME_DIR/apt/lists/partial" "$FIFO_RUNTIME_DIR/apt/archives/partial" \
    "$FIFO_RUNTIME_DIR/packages" "$FIFO_RUNTIME_DIR/sysroot"
if [[ ! -e "$FIFO_ODOO_DIR" ]]; then
    git init "$FIFO_ODOO_DIR"
    git -C "$FIFO_ODOO_DIR" remote add origin https://github.com/odoo/odoo.git
    git -C "$FIFO_ODOO_DIR" fetch --depth 1 origin "$FIFO_ODOO_COMMIT"
    git -C "$FIFO_ODOO_DIR" checkout --detach FETCH_HEAD
fi
test "$(git -C "$FIFO_ODOO_DIR" rev-parse HEAD)" = "$FIFO_ODOO_COMMIT"
test -z "$(git -C "$FIFO_ODOO_DIR" status --porcelain --untracked-files=no)"
cat > "$FIFO_RUNTIME_DIR/apt/sources.list" <<'SOURCES'
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie main
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://security.debian.org/debian-security trixie-security main
SOURCES
FIFO_APT_OPTIONS=(
    -o "Dir::Etc::sourcelist=$FIFO_RUNTIME_DIR/apt/sources.list" -o Dir::Etc::sourceparts=-
    -o "Dir::State::lists=$FIFO_RUNTIME_DIR/apt/lists"
    -o "Dir::Cache::archives=$FIFO_RUNTIME_DIR/apt/archives" -o APT::Sandbox::User=agent
)
apt-get "${FIFO_APT_OPTIONS[@]}" update
(
    cd "$FIFO_RUNTIME_DIR/packages"
    apt-get "${FIFO_APT_OPTIONS[@]}" download postgresql-17 postgresql-client-17 \
        libpq-dev libpq5 libldap-dev libldap2 libsasl2-dev libsasl2-2
    for FIFO_DEB_FILE in ./*.deb; do
        dpkg-deb -x "$FIFO_DEB_FILE" "$FIFO_RUNTIME_DIR/sysroot"
    done
)
export UV_CACHE_DIR="$FIFO_RUNTIME_DIR/uv-cache"
export CC=gcc
export PATH="$FIFO_RUNTIME_DIR/sysroot/usr/bin:$PATH"
export CFLAGS="-I$FIFO_RUNTIME_DIR/sysroot/usr/include -I$FIFO_RUNTIME_DIR/sysroot/usr/include/postgresql"
export LDFLAGS="-L$FIFO_RUNTIME_DIR/sysroot/usr/lib/x86_64-linux-gnu -Wl,-rpath,$FIFO_RUNTIME_DIR/sysroot/usr/lib/x86_64-linux-gnu"
uv venv --allow-existing --python python3 "$FIFO_RUNTIME_DIR/venv"
uv pip install --python "$FIFO_PYTHON" -r "$FIFO_ODOO_DIR/requirements.txt"
# Older cached builds may have linked static libpq before the shared library existed.
if ! "$FIFO_PYTHON" -c 'import psycopg2'; then
    uv pip install --python "$FIFO_PYTHON" --no-cache --reinstall psycopg2==2.9.9
fi
"$FIFO_PYTHON" -c 'import ldap, psycopg2'
if [[ ! -f "$FIFO_RUNTIME_DIR/pgdata/PG_VERSION" ]]; then
    "$FIFO_PG_BIN/initdb" -D "$FIFO_RUNTIME_DIR/pgdata" --auth-local=trust \
        --auth-host=scram-sha-256 --encoding=UTF8 --locale=C.UTF-8
fi
fifo_start_database
fifo_odoo --without-demo -i company_financial_cutover --stop-after-init --no-http \
    --logfile="$FIFO_RUNTIME_DIR/install.log"
echo "Odoo 19 and Company Financial Cutover installed in the isolated development database."
