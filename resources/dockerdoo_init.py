#!/usr/bin/env python3
"""Prepare PostgreSQL and the database before the Odoo server starts. See "Hosted platforms" in the README."""

import os
import secrets
import subprocess
import sys
import time

import psycopg2
from odoo.modules.module import Manifest
from odoo.tools import config, parse_version
from psycopg2 import sql

env = os.environ
dbname = env.get("PGDATABASE", "")


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
if dbname and "1" in (env.get("AUTO_INIT"), env.get("AUTO_UPGRADE")):
    cur = conn.cursor()
    # Containers started together change the database once.
    cur.execute("SELECT pg_advisory_lock(727383001)")
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    exists = cur.fetchone()
    try:
        if env.get("AUTO_INIT") == "1" and not exists:
            if not env.get("INIT_ADMIN_PASSWORD"):
                sys.exit("AUTO_INIT needs INIT_ADMIN_PASSWORD")
            # odoo db init only takes the password as an argument, so it gets a throwaway one
            # and the shell below sets the real one from the environment.
            init = ["db", "init", dbname, "--language", env.get("INIT_LANGUAGE", "en_US"),
                    "--username", env.get("INIT_ADMIN_LOGIN", "admin"), "--password", secrets.token_hex(24)]
            odoo(*init, *(["--country", env["INIT_COUNTRY"]] if env.get("INIT_COUNTRY") else []))
            try:
                modules = [m.strip() for m in env.get("INIT_MODULES", "").split(",") if m.strip()]
                if modules:
                    odoo("module", "install", "-d", dbname, *modules)
                odoo("shell", "-d", dbname, "--no-http", stdin="import os\n"
                     "env.ref('base.user_admin').password = os.environ['INIT_ADMIN_PASSWORD']\nenv.cr.commit()\n")
            except subprocess.CalledProcessError:
                # Start over on the next boot rather than serve a half-installed database.
                odoo("db", "drop", dbname)
                raise
        elif env.get("AUTO_UPGRADE") == "1" and exists:
            # odoo module upgrade reads records before it updates the schema, so a release that adds a
            # stored field fails there; odoo server -u updates the schema first.
            config.parse_config([], setup_logging=False)
            with psycopg2.connect(dbname=dbname) as db, db.cursor() as dbcur:
                dbcur.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
                outdated = [name for name, version in dbcur.fetchall()
                            if (manifest := Manifest.for_addon(name, display_warning=False))
                            and parse_version(manifest.version) > parse_version(version)]
            if outdated:
                odoo("server", "-d", dbname, "-u", ",".join(outdated), "--stop-after-init", "--no-http")
    except subprocess.CalledProcessError as error:
        sys.exit(f"odoo {' '.join(error.cmd[1:3])} failed; the server does not start")
conn.close()
