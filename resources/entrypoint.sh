#!/bin/bash
set -e

# Platforms such as Railway mount volumes owned by root. Hand the data directory to the Odoo user
# and continue without root.
if [ "$(id -u)" = 0 ] && [ "${ODOO_RUN_AS_ROOT:-0}" != 1 ]; then
    mkdir -p "$ODOO_DATA_DIR"
    # ponytail: the directory's owner marks the whole tree; chown -R only on first use.
    [ "$(stat -c %u "$ODOO_DATA_DIR")" = "$APP_UID" ] || chown -R "$APP_UID:$APP_GID" "$ODOO_DATA_DIR"
    chown "$APP_UID:$APP_GID" /etc/odoo
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups env HOME="$ODOO_BASEPATH" "$0" "$@"
fi

# Odoo 20 reads ODOO_<OPTION> and PG* variables itself. Only derived defaults are set here.
export ODOO_HTTP_PORT="${ODOO_HTTP_PORT:-${PORT:-8069}}"
if [ -n "$PGDATABASE" ]; then export ODOO_DBFILTER="${ODOO_DBFILTER:-^${PGDATABASE}\$}"; fi
export ODOO_ADMIN_PASSWD="${ODOO_ADMIN_PASSWD:-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
extra="$(python3 /getaddons.py "$ODOO_EXTRA_ADDONS")"
export ODOO_ADDONS_PATH="${ODOO_ADDONS_PATH:-${extra:+$extra,}$ODOO_BASEPATH/addons}"
# Odoo's thread pool and memory limits assume a dedicated host; fit them to the container (see the README).
limits="$(python3 /containerlimits.py)"
eval "$limits"
# Add-on options have no ODOO_<OPTION> variable; they are read from the configuration file.
if [ -n "$ODOO_EXTRA_OPTIONS" ]; then printf '[options]\n%s\n' "$ODOO_EXTRA_OPTIONS" > "$ODOO_RC"; fi

if [ "$1" = odoo ] && { [ $# = 1 ] || [ "$2" = server ]; }; then
    python3 /dockerdoo_init.py
    unset DB_ADMIN_URL INIT_ADMIN_PASSWORD
fi
exec "$@"
