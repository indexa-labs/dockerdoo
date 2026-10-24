#!/usr/bin/env python3
"""Prepare PostgreSQL and the database before the Odoo server starts. See "Hosted platforms" in the README."""

import os
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import closing

import psycopg2
from odoo.modules.module import Manifest
from odoo.tools import config, parse_version
from psycopg2 import sql
from psycopg2.extensions import make_dsn

env = os.environ
dbname = env.get("PGDATABASE", "")
baseline = env.get("BASELINE_DATABASE", "")
if baseline and baseline == dbname:
    sys.exit("BASELINE_DATABASE names the saved copy, so it must differ from PGDATABASE")


def connect(dsn=None):
    deadline = time.monotonic() + int(env.get("DB_WAIT_TIMEOUT", "60"))
    while True:
        try:
            conn = psycopg2.connect(dsn or "dbname=postgres", connect_timeout=10)
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def odoo(*args, stdin=None):
    subprocess.run(["odoo", *args], input=stdin, text=True, check=True)


def filestore(name):
    return os.path.join(env.get("ODOO_DATA_DIR", ""), "filestore", name)


def copy_database(cur, source, target):
    """Replace target and its files with an exact copy of source, database.uuid included.

    The copy is made under another name first, so a copy that fails leaves target as it was.
    """
    staging = f"{target}_copy"
    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(staging)))
    cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(staging), sql.Identifier(source)))
    shutil.rmtree(filestore(staging), ignore_errors=True)
    if os.path.isdir(filestore(source)):
        # Odoo never rewrites a stored file, so the copy can share them.
        shutil.copytree(filestore(source), filestore(staging), copy_function=os.link)
    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(target)))
    cur.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(staging), sql.Identifier(target)))
    shutil.rmtree(filestore(target), ignore_errors=True)
    if os.path.isdir(filestore(staging)):
        os.rename(filestore(staging), filestore(target))


def database_exists(cur, name):
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    return bool(cur.fetchone())


def enable_pgvector():
    # Odoo's AI apps create the extension when they install, which a role that is not a superuser cannot do.
    if not env.get("DB_ADMIN_URL"):
        return
    try:
        admin = connect(make_dsn(env["DB_ADMIN_URL"], dbname=dbname))
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
            if cur.fetchone():
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        admin.close()
    except psycopg2.Error as error:
        print(f"pgvector is available but could not be enabled: {error}", file=sys.stderr)


if env.get("DB_ADMIN_URL"):
    # Odoo refuses to run as postgres, and platforms often hand out only a superuser.
    role, password = env.get("PGUSER", ""), env.get("PGPASSWORD", "")
    if role in ("", "postgres") or not password:
        sys.exit("DB_ADMIN_URL needs PGUSER (not postgres) and PGPASSWORD for the application role")
    admin = connect(env["DB_ADMIN_URL"])
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        verb = "ALTER" if cur.fetchone() else "CREATE"
        cur.execute(sql.SQL(verb + " ROLE {} LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %s")
                    .format(sql.Identifier(role)), (password,))
    admin.close()

conn = connect()
if dbname and (env.get("DB_ADMIN_URL") or baseline or "1" in (env.get("AUTO_INIT"), env.get("AUTO_UPGRADE"))):
    cur = conn.cursor()
    # Containers started together change the database once.
    cur.execute("SELECT pg_advisory_lock(727383001)")
    exists = database_exists(cur, dbname)
    saving = baseline and env.get("SAVE_BASELINE") == "1"
    try:
        if baseline and not saving:
            if database_exists(cur, baseline):
                copy_database(cur, baseline, dbname)
                exists = True
                print(f"Restored {dbname} from its baseline, {baseline}")
            elif exists:
                print(f"{baseline} is not saved yet; start once with SAVE_BASELINE=1 to save {dbname} as it")
        if exists:
            enable_pgvector()
        if env.get("AUTO_INIT") == "1" and not exists:
            if not env.get("INIT_ADMIN_PASSWORD"):
                sys.exit("AUTO_INIT needs INIT_ADMIN_PASSWORD")
            dump = env.get("INIT_DUMP")
            if not dump:
                # odoo db init only takes the password as an argument, so it gets a throwaway one
                # and the shell below sets the real one from the environment.
                init = ["db", "init", dbname, "--language", env.get("INIT_LANGUAGE", "en_US"),
                        "--username", env.get("INIT_ADMIN_LOGIN", "admin"), "--password", secrets.token_hex(24)]
                odoo(*init, *(["--country", env["INIT_COUNTRY"]] if env.get("INIT_COUNTRY") else []),
                     *(["--with-demo"] if env.get("INIT_WITH_DEMO") == "1" else []))
                enable_pgvector()  # Before the modules install, because an AI app's install hook needs it.
            setup = "import os\nenv.ref('base.user_admin').password = os.environ['INIT_ADMIN_PASSWORD']\n"
            try:
                if dump:
                    odoo("db", "load", dbname, dump)
                    enable_pgvector()
                    if env.get("INIT_ADMIN_LOGIN"):
                        setup += "env.ref('base.user_admin').login = os.environ['INIT_ADMIN_LOGIN']\n"
                else:
                    modules = [m.strip() for m in env.get("INIT_MODULES", "").split(",") if m.strip()]
                    if modules:
                        odoo("module", "install", "-d", dbname, *modules)
                odoo("shell", "-d", dbname, "--no-http", stdin=setup + "env.cr.commit()\n")
            except subprocess.CalledProcessError:
                # Start over on the next boot rather than serve a half-installed database.
                odoo("db", "drop", dbname)
                raise
        elif env.get("AUTO_UPGRADE") == "1" and exists:
            # odoo module upgrade reads records before it updates the schema, so a release that adds a
            # stored field fails there; odoo server -u updates the schema first.
            config.parse_config([], setup_logging=False)
            # The connection's context only ends the transaction; closing it lets SAVE_BASELINE copy the database.
            with closing(psycopg2.connect(dbname=dbname)) as db, db.cursor() as dbcur:
                dbcur.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
                outdated = [name for name, version in dbcur.fetchall()
                            if (manifest := Manifest.for_addon(name, display_warning=False))
                            and parse_version(manifest.version) > parse_version(version)]
            if outdated:
                odoo("server", "-d", dbname, "-u", ",".join(outdated), "--stop-after-init", "--no-http")
            else:
                # odoo server -u refreshes the apps list; without it, add-ons new to the image stay unlisted.
                odoo("shell", "-d", dbname, "--no-http", stdin="env['ir.module.module'].update_list()\nenv.cr.commit()\n")
        if saving and database_exists(cur, dbname):
            copy_database(cur, dbname, baseline)
            print(f"Saved {dbname} as its baseline, {baseline}")
    except subprocess.CalledProcessError as error:
        sys.exit(f"Preparing the database failed in odoo {error.cmd[1]}; the server does not start")
conn.close()
