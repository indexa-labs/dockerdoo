#!/usr/bin/env python3
"""Prepare PostgreSQL and the database before the Odoo server starts.

Runs as the Odoo user from the entrypoint, after the configuration file exists:

- waits for PostgreSQL (``DB_WAIT_TIMEOUT`` seconds);
- with ``DB_ADMIN_URL``, creates or updates ``PGUSER`` as a login role that may create
  databases but is not a superuser, because Odoo refuses to run as ``postgres``;
- with ``AUTO_INIT=1``, creates ``DBNAME`` when it is missing and installs ``INIT_MODULES``;
- with ``AUTO_UPGRADE=1``, upgrades modules whose manifest version is newer than the database.

Database work runs under a PostgreSQL advisory lock, so containers started together never
initialize or upgrade the same database twice. Nothing here imports Odoo, so a start with
nothing to do costs a few queries.
"""

import ast
import collections
import configparser
import contextlib
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse

import psycopg2
from psycopg2 import sql

LOCK_KEY = 727383001
TIMINGS = []


def log(message):
    print(f"[dockerdoo] {message}", flush=True)


def flag(name):
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def connect(dbname, **kwargs):
    return psycopg2.connect(dbname=dbname, connect_timeout=10, **kwargs)


@contextlib.contextmanager
def session(dbname):
    """A cursor that commits and always closes: an open session blocks DROP and RENAME later."""
    conn = connect(dbname)
    try:
        with conn, conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def wait_for_postgres(dbname, admin_url=None):
    timeout = int(os.environ.get("DB_WAIT_TIMEOUT", "60"))
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        if admin_url:
            attempts = [lambda: psycopg2.connect(admin_url, connect_timeout=10)]
        else:
            attempts = [lambda name=name: connect(name) for name in filter(None, (dbname, "postgres", "template1"))]
        for attempt in attempts:
            try:
                attempt().close()
                return
            except psycopg2.OperationalError as exc:
                last = str(exc).strip()
                # A missing database still proves that the server answers with these credentials.
                if "does not exist" in last and not admin_url:
                    return
        if time.monotonic() > deadline:
            sys.exit(f"[dockerdoo] PostgreSQL is not reachable after {timeout} s: {last[:200]}")
        time.sleep(1)


def ensure_app_role():
    url = os.environ.get("DB_ADMIN_URL")
    if not url:
        return
    role, password = os.environ.get("PGUSER", ""), os.environ.get("PGPASSWORD", "")
    if not role or role == "postgres" or not password:
        sys.exit("[dockerdoo] DB_ADMIN_URL needs PGUSER (not postgres) and PGPASSWORD for the application role")
    parsed = urllib.parse.urlparse(url)
    if urllib.parse.unquote(parsed.username or "") == role:
        sys.exit("[dockerdoo] PGUSER must differ from the DB_ADMIN_URL user")
    conn = psycopg2.connect(url, connect_timeout=10)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            verb = "ALTER" if cur.fetchone() else "CREATE"
            cur.execute(
                sql.SQL(verb + " ROLE {} WITH LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %s").format(
                    sql.Identifier(role)
                ),
                (password,),
            )
    finally:
        conn.close()
    log(f"application role {role!r}: {verb.lower()}d without superuser rights")


def check_role():
    with session("postgres") as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
    if row and row[0]:
        log("warning: PGUSER is a PostgreSQL superuser; set DB_ADMIN_URL to create a restricted role")


class PhaseFailed(Exception):
    def __init__(self, phase, code, tail):
        super().__init__(f"{phase} failed with exit code {code}")
        self.phase, self.code, self.tail = phase, code, tail


def run_phase(phase, command, stdin=None):
    log(f"{phase}: {' '.join('***' if previous == '--password' else part for previous, part in zip([''] + command, command))}")
    started = time.monotonic()
    tail = collections.deque(maxlen=200)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if stdin is not None:
        process.stdin.write(stdin)
        process.stdin.close()
    for line in process.stdout:
        sys.stdout.write(line)
        tail.append(line.rstrip("\n"))
    code = process.wait()
    seconds = round(time.monotonic() - started, 2)
    TIMINGS.append(f"{phase}={seconds}s")
    if code:
        raise PhaseFailed(phase, code, list(tail))
    return seconds


def summary(failure):
    lines = [line for line in failure.tail if " ERROR " in line or " CRITICAL " in line or "Error" in line]
    return " | ".join(lines[-3:] or failure.tail[-3:])[-600:]


def database_state(dbname):
    try:
        connect(dbname).close()
    except psycopg2.OperationalError as exc:
        if "does not exist" in str(exc):
            return "missing"
        raise
    with session(dbname) as cur:
        cur.execute("SELECT to_regclass('public.ir_module_module')")
        if cur.fetchone()[0] is None:
            # Odoo 20 creates its orm_signaling_* tables before base is installed.
            cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename !~ '^orm_signaling_'")
            return "empty" if cur.fetchone()[0] == 0 else "foreign"
        cur.execute("SELECT state FROM ir_module_module WHERE name = 'base'")
        row = cur.fetchone()
        return "ready" if row and row[0] == "installed" else "incomplete"


def series():
    release = os.path.join(os.environ.get("ODOO_BASEPATH", "/opt/odoo"), "odoo", "release.py")
    with open(release, encoding="utf-8") as handle:
        match = re.search(r"^version_info\s*=\s*\(\s*(\d+)\s*,\s*(\d+)", handle.read(), re.M)
    return f"{match.group(1)}.{match.group(2)}"


def version_key(version):
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def disk_index():
    """Module manifests in the order Odoo searches: installed odoo/addons packages, core, then addons_path."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.environ.get("ODOO_RC", "/etc/odoo/odoo.conf"))
    configured = parser.get("options", "addons_path", fallback="")
    base = os.environ.get("ODOO_BASEPATH", "/opt/odoo")
    roots = [os.path.join(entry, "odoo", "addons") for entry in sys.path if entry]
    roots += [os.path.join(base, "odoo", "addons"), os.path.join(base, "addons")]
    roots += [path.strip() for path in configured.split(",") if path.strip()]
    index = {}
    for root in dict.fromkeys(roots):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            manifest = os.path.join(root, name, "__manifest__.py")
            if name not in index and os.path.isfile(manifest):
                index[name] = manifest
    return index


def plan(dbname, wanted):
    index = disk_index()
    unknown = [name for name in wanted if name not in index]
    if unknown:
        sys.exit(f"[dockerdoo] INIT_MODULES not found on the add-ons path: {', '.join(unknown)}")
    with session(dbname) as cur:
        cur.execute("SELECT name, state, latest_version FROM ir_module_module")
        modules = {name: (state, version) for name, state, version in cur.fetchall()}
    prefix = series() + "."
    install = [name for name in wanted if modules.get(name, ("uninstalled", None))[0] != "installed"]
    upgrade = []
    for name, (state, version) in sorted(modules.items()):
        if state == "to upgrade":
            upgrade.append(name)
        elif state == "installed" and name in index and version:
            with open(index[name], encoding="utf-8") as handle:
                disk = ast.literal_eval(handle.read()).get("version") or "1.0"
            disk = disk if disk.startswith(prefix) else prefix + disk
            if version_key(disk) > version_key(version):
                upgrade.append(name)
    unlisted = sorted(name for name in index if name not in modules)
    return install, upgrade, unlisted


def initialise(dbname, state):
    if not os.environ.get("INIT_ADMIN_PASSWORD"):
        sys.exit("[dockerdoo] INIT_ADMIN_PASSWORD is required to create the database")
    if state == "foreign":
        sys.exit(f"[dockerdoo] database {dbname!r} has tables but is not an Odoo database; refusing to change it")
    # The real password reaches Odoo through the environment, never through the command line.
    # Hexadecimal, because a URL-safe token can start with "-", which the CLI reads as an option.
    throwaway = secrets.token_hex(24)
    command = ["odoo", "db", "init", dbname, "--language", os.environ.get("INIT_LANGUAGE", "en_US")]
    command += ["--username", os.environ.get("INIT_ADMIN_LOGIN", "admin"), "--password", throwaway]
    if os.environ.get("INIT_COUNTRY"):
        command += ["--country", os.environ["INIT_COUNTRY"]]
    if state in ("empty", "incomplete"):
        # Recreates a database that holds no Odoo data yet; the role must own it.
        command.append("--force")
    run_phase("db_init", command)


SHELL_PASSWORD = """
import os
env.ref('base.user_admin').password = os.environ['INIT_ADMIN_PASSWORD']
"""

SHELL_LANGUAGES = """
import os
codes = [code.strip() for code in os.environ['INIT_LOAD_LANGUAGES'].split(',') if code.strip()]
for code in codes:
    env['res.lang']._activate_lang(code)
env.ref('base.user_admin').lang = codes[0]
env.ref('base.partner_admin').lang = codes[0]
"""


def odoo_shell(phase, dbname, script):
    run_phase(phase, ["odoo", "shell", "-d", dbname, "--no-http"], stdin=script + "\nenv.cr.commit()\n")


def prepare(dbname):
    created = False
    if flag("AUTO_INIT"):
        state = database_state(dbname)
        log(f"database {dbname!r}: {state}")
        if state != "ready":
            initialise(dbname, state)
            created = True
    if not created and database_state(dbname) != "ready":
        return
    wanted = [name.strip() for name in os.environ.get("INIT_MODULES", "base").split(",") if name.strip()] if flag("AUTO_INIT") else []
    install, upgrade, unlisted = plan(dbname, wanted)
    if not flag("AUTO_UPGRADE"):
        upgrade = []
    log(f"plan: install={install} upgrade={upgrade} new_on_disk={len(unlisted)}")
    if unlisted:
        odoo_shell("update_apps_list", dbname, "env['ir.module.module'].update_list()")
    if install:
        run_phase("module_install", ["odoo", "module", "install", "-d", dbname, *install])
    if upgrade:
        snapshot = f"{dbname}__pre_upgrade" if flag("AUTO_UPGRADE_SNAPSHOT") else None
        if snapshot:
            run_phase("snapshot", ["odoo", "db", "duplicate", "-f", dbname, snapshot])
        try:
            run_phase("module_upgrade", ["odoo", "module", "upgrade", "-d", dbname, "--outdated", "all"])
        except PhaseFailed:
            if snapshot:
                run_phase("restore_snapshot", ["odoo", "db", "rename", "-f", snapshot, dbname])
                log("upgrade failed; restored the database copied before the upgrade")
            raise
        if snapshot:
            run_phase("drop_snapshot", ["odoo", "db", "drop", snapshot])
    if created:
        odoo_shell("admin_password", dbname, SHELL_PASSWORD)
        if os.environ.get("INIT_LOAD_LANGUAGES"):
            odoo_shell("languages", dbname, SHELL_LANGUAGES)


def main():
    started = time.monotonic()
    dbname = os.environ.get("DBNAME") or ""
    if os.environ.get("DB_ADMIN_URL"):
        wait_for_postgres(dbname, os.environ["DB_ADMIN_URL"])
        ensure_app_role()
    wait_for_postgres(dbname)
    check_role()
    if not (flag("AUTO_INIT") or flag("AUTO_UPGRADE")):
        return
    if not dbname:
        sys.exit("[dockerdoo] AUTO_INIT and AUTO_UPGRADE need DBNAME")
    lock = connect("postgres")
    lock.autocommit = True
    try:
        lock.cursor().execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        prepare(dbname)
    except PhaseFailed as failure:
        log(f"{failure.phase} failed (exit {failure.code}): {summary(failure)}")
        log("exiting before the server starts")
        sys.exit(3)
    finally:
        lock.close()
    log(f"database ready in {round(time.monotonic() - started, 2)} s ({' '.join(TIMINGS) or 'nothing to do'})")


if __name__ == "__main__":
    main()
