# Dockerized Odoo 20

An Odoo 20 Community image for hosted platforms such as Railway, Render, or Kubernetes, and for local Docker Compose. This fork of [iterativo-git/dockerdoo](https://github.com/iterativo-git/dockerdoo) configures Odoo through the environment variables Odoo 20 reads natively and adds only what platforms need before Odoo starts.

## Image

`ghcr.io/indexa-labs/dockerdoo:20.0` is the newest tested build, and `20.0.YYYYMMDD` names one build. The dated tag is valid semver, so platforms that auto-update by semver follow dated builds. Pin a dated tag or a digest in production, and verify it with:

```shell
gh attestation verify oci://ghcr.io/indexa-labs/dockerdoo:20.0 --owner indexa-labs
```

GitHub Actions builds the current Odoo `20.0` branch weekly and on every change, for `linux/amd64` and `linux/arm64`. It publishes only after `tests/test_hosted.py` and Odoo's own base tests pass on both. The image installs Odoo's pinned Python requirements and has no `sudo` or developer tools. It is 2.8 GB unpacked and 0.54 GB compressed, and the newest `postgresql-client` is installed so that `odoo db dump` works against current servers.

`20.0-dev` and `20.0.YYYYMMDD-dev` are the matching development builds, from the Dockerfile's `development` target. They add what working on add-ons inside the container needs: `git`, `npm`, `ffmpeg` for Odoo's browser-test screencasts, compilers and `-dev` headers so `pip install` can build add-on requirements, `watchdog` for `--dev=reload`, `debugpy`, and passwordless `sudo` for the odoo user. Use them for development only.

## Configuration

Odoo 20 reads every server option from `ODOO_<OPTION>` (for example `ODOO_WORKERS`, `ODOO_PROXY_MODE`, `ODOO_SMTP_SERVER`) and the database connection from `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, and `PGDATABASE`. The image sets only these defaults:

| Variable | Default |
| --- | --- |
| `ODOO_HTTP_PORT` | The platform's `PORT`, else `8069` |
| `ODOO_DBFILTER` | `^$PGDATABASE$` when `PGDATABASE` is set |
| `ODOO_ADMIN_PASSWD` | A random master password per container |
| `ODOO_MAX_HTTP_THREADS` | One thread per 16 MiB of the container's memory, from 32 to 256 |
| `ODOO_LIMIT_MEMORY_SOFT`, `ODOO_LIMIT_MEMORY_HARD` | 70 % and 90 % of the container's memory, plus 8 MiB for each HTTP thread |
| `ODOO_LIST_DB` | `False` |
| `ODOO_HTTP_INTERFACE` | `0.0.0.0` (Odoo's own default is `127.0.0.1`) |
| `ODOO_ADDONS_PATH` | Add-on folders found under `/mnt/extra-addons`, then Odoo's add-ons |
| `ODOO_DATA_DIR` | `/var/lib/odoo/data`; mount the volume here |
| `ODOO_LOG_CONFIG` | Not set, so Odoo logs text to stderr. `/dockerdoo_logging.json` logs JSON to stdout; see [Logging](#logging) |

Add-on options that Odoo has no variable for go in `ODOO_EXTRA_OPTIONS`, one `key = value` per line; the image writes them to the configuration file.

The last three rows fit Odoo to its container. In threaded mode (`ODOO_WORKERS` unset or `0`), each open websocket holds one HTTP thread, and Odoo sizes its pool at twice the host's CPUs plus one, whatever the container may use. Odoo's memory limits default to 2048 and 2560 MiB and count virtual memory, which includes the 8 MiB stack of every thread. The image reads the container's memory limit (the machine's memory when there is none, as on virtual machines) and derives all three from it. A variable you set always wins. With `ODOO_WORKERS` above zero the memory limits apply to each worker, so the image leaves them to you, and on a machine of 16 GiB or more it leaves all three to Odoo's own defaults.

## Start step

Before the server starts, the image does what hosted platforms need and Odoo does not:

| Variable | Effect |
| --- | --- |
| `DB_WAIT_TIMEOUT` | Seconds to wait for PostgreSQL (default `60`) |
| `DB_ADMIN_URL` | PostgreSQL administrator URL. Creates or updates `PGUSER` with `PGPASSWORD` as a role that may create databases but is not a superuser, because Odoo refuses to run as `postgres`. It is removed from the server's environment |
| `AUTO_INIT` | `1` creates `PGDATABASE` when it is missing (`odoo db init`) and installs `INIT_MODULES` (`odoo module install`), or loads it from `INIT_DUMP`. If setup fails, the database is dropped so the next start begins again |
| `INIT_MODULES` | Comma-separated modules to install in a new database |
| `INIT_LANGUAGE`, `INIT_COUNTRY` | Language and country of a new database (default `en_US`) |
| `INIT_DUMP` | Path of a dump zip, as `odoo db dump` writes it. A new database is loaded from it (`odoo db load`) instead of created and installed, so `INIT_MODULES`, `INIT_LANGUAGE`, and `INIT_COUNTRY` do not apply. The copy gets its own `database.uuid` and `database.secret`. `INIT_ADMIN_PASSWORD` replaces the administrator's password, and `INIT_ADMIN_LOGIN`, when set, replaces the login. Everything else stays as the dump has it. `AUTO_UPGRADE` updates modules that are older in the dump than in the image only from the next start, so build the dump with the same image and add-ons |
| `INIT_ADMIN_LOGIN`, `INIT_ADMIN_PASSWORD` | Administrator of a new database. The password is set through Odoo, never on a command line |
| `AUTO_UPGRADE` | `1` updates, at each start, the installed modules whose version on disk is newer than the database's (`odoo server -u <modules> --stop-after-init`), so new fields get their columns before Odoo reads any records. Each start also refreshes the apps list, so add-ons new to the image appear in Apps. A failure stops the container before the server starts |

Database creation and upgrades run under a PostgreSQL advisory lock, so containers started together change a database once. The container starts as root, takes ownership of `ODOO_DATA_DIR` if needed, and drops to the `odoo` user; set `ODOO_RUN_AS_ROOT=1` to keep root, or start it as uid 1000 on a volume it already owns.

Odoo 20 serves websockets on a separate port when `ODOO_WORKERS` is above zero. A platform that routes one port per service needs `ODOO_WORKERS=0` or a reverse proxy in front of both ports.

## Logging

Odoo writes its log to stderr, and hosts such as Railway show every stderr line as an error, whatever its level. Those hosts read structured logs instead: one JSON object per line on stdout, with a `message` and a `level`. Set `ODOO_LOG_CONFIG=/dockerdoo_logging.json` on such a host, and Odoo writes each record that way:

```json
{"time": "2026-10-22T20:40:10.246+00:00", "level": "info", "logger": "odoo.registry", "dbname": "jsonlog", "message": "module base: creating or updating database tables"}
```

`level` is `debug`, `info`, `warn`, or `error`: Python's `WARNING` becomes `warn`, and `CRITICAL` becomes `error`. A traceback is part of its record's `message`, so a record stays on one line. `ODOO_LOG_LEVEL` and `ODOO_LOG_HANDLER` keep setting the levels, from the default `INFO`. With this configuration, Odoo ignores `ODOO_LOGFILE`, `ODOO_SYSLOG`, and `ODOO_LOG_DB`.

## Behind a proxy

With `ODOO_PROXY_MODE=True`, Odoo 20 trusts `X-Forwarded-For` and `X-Forwarded-Proto` only on a request that also carries `X-Forwarded-Host` (`odoo/http/router.py` and `odoo/http/server.py`). Railway, nginx, and Caddy send it. Fly.io and AWS load balancers do not, and behind them Odoo sees plain `http` and the proxy's address: redirects use `http`, and the login cooldown counts every user as one client. The image does not work around this, because Odoo's maintainers keep the check on purpose: it shows that the proxy forces the header instead of passing a client's (odoo/odoo#63277, #70117). Where the platform does not send it, put a proxy that sets `X-Forwarded-Host` to the `Host` it received in front of Odoo. The right fix is in Odoo: trust the client address and scheme whenever proxy mode is on, and keep trusting the host only when the header is present.

## Add-ons

Add-ons in `./custom` are copied into the image at build time, and folders mounted under `/mnt/extra-addons` are found at start. To ship your own add-ons and Python dependencies, build on a pinned digest:

```dockerfile
FROM ghcr.io/indexa-labs/dockerdoo:20.0.YYYYMMDD@sha256:...
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
COPY --chown=odoo:odoo my_addons/ /mnt/extra-addons/my_addons/
ENV AUTO_INIT=1 INIT_MODULES=my_module
```

## Local use

Copy `.env.example` to `.env`, set the passwords, and run `docker compose up`. Odoo answers on `http://127.0.0.1:8069`.

For development, set `target: development` under `build` in `docker-compose.yml`, or use `image: ghcr.io/indexa-labs/dockerdoo:20.0-dev`.

To test a local build the way CI does:

```shell
docker build --target production --build-arg ODOO_VERSION=20.0 -t dockerdoo-local:20.0 .
python3 tests/test_hosted.py --image dockerdoo-local:20.0 --native-tests
```

## Credits

Based on [iterativo-git/dockerdoo](https://github.com/iterativo-git/dockerdoo), itself mainly based on dockery-odoo work by [David Arnold](https://github.com/blaggacao) ([XOE Solutions](https://xoe.solutions)), with ideas from [Odoo](https://github.com/odoo) ([docker](https://github.com/odoo/docker)), [OCA](https://github.com/OCA) ([maintainer-quality-tools](https://github.com/OCA/maintainer-quality-tools)), and [Ingeniería ADHOC](https://github.com/jjscarafia) ([docker-odoo-adhoc](https://github.com/ingadhoc/docker-odoo-adhoc)).
