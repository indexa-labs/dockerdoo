"""Test the image the way hosted platforms run it.

Covers a root-owned data volume, a platform PORT, Odoo starting before PostgreSQL, a
superuser-only PostgreSQL that needs a restricted application role, automatic database
creation, restarts, concurrent starts, and automatic upgrades of outdated modules.
"""

import argparse
import json
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', required=True)
parser.add_argument('--postgres', default='postgres:16')
args = parser.parse_args()

tag = 'dkhosted-' + secrets.token_hex(3)
net, pg, volume = tag + '-net', tag + '-pg', tag + '-data'
admin_password, app_password, odoo_password = secrets.token_hex(12), secrets.token_hex(12), secrets.token_hex(12)
addons = Path(tempfile.mkdtemp(prefix='dkhosted-addons-'))
logs = Path(tempfile.mkdtemp(prefix='dkhosted-logs-'))
secrets_ = [admin_password, app_password, odoo_password]
result = {'image': args.image}
containers = []


def redact(text):
    for value in secrets_:
        text = text.replace(value, '<REDACTED>')
    return text


def sh(cmd, check=True, input=None, timeout=900):
    r = subprocess.run(cmd, text=True, capture_output=True, input=input, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(redact(' '.join(cmd[:4]) + ': ' + (r.stdout + r.stderr)[-2000:]))
    return r


def psql(query, user='pgadmin', password=admin_password, db='postgres'):
    return sh(['docker', 'exec', '-e', 'PGPASSWORD=' + password, pg, 'psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1',
               '-h', '127.0.0.1', '-U', user, '-d', db, '-c', query]).stdout.strip()


def module(version):
    root = addons / 'probe' / 'probe_hosted'
    root.mkdir(parents=True, exist_ok=True)
    (root / '__init__.py').write_text('')
    (root / '__manifest__.py').write_text(repr({'name': 'Probe hosted', 'version': version, 'depends': ['base'],
                                                'installable': True, 'license': 'LGPL-3'}))


def odoo(name, dbname, *extra, root=True, port='9000'):
    cmd = ['docker', 'run', '-d', '--name', name, '--network', net,
           '-v', volume + ':/srv/odoo', '-v', str(addons) + ':/mnt/extra-addons:ro',
           '-e', 'ODOO_DATA_DIR=/srv/odoo/data', '-e', 'ODOO_LOGS_DIR=/srv/odoo/logs',
           '-e', 'PORT=' + port, '-e', 'PGHOST=' + pg, '-e', 'PGPORT=5432',
           '-e', 'PGUSER=odoo_app', '-e', 'PGPASSWORD=' + app_password,
           '-e', f'DB_ADMIN_URL=postgresql://pgadmin:{admin_password}@{pg}:5432/postgres',
           '-e', 'DBNAME=' + dbname, '-e', 'AUTO_INIT=1', '-e', 'AUTO_UPGRADE=1',
           '-e', 'INIT_MODULES=base,contacts,probe_hosted', '-e', 'INIT_ADMIN_PASSWORD=' + odoo_password,
           '--health-interval', '2s']
    if root:
        cmd += ['--user', '0']
    cmd += [*extra, args.image]
    sh(cmd)
    containers.append(name)


def wait_healthy(name, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = sh(['docker', 'inspect', '-f', '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}', name]).stdout.split()
        if state[0] != 'running':
            raise RuntimeError(f'{name} stopped: ' + redact(sh(['docker', 'logs', name], check=False).stdout[-3000:]))
        if len(state) > 1 and state[1] == 'healthy':
            return round(timeout - (deadline - time.monotonic()), 1)
        time.sleep(1)
    raise RuntimeError(f'{name} not healthy after {timeout} s')


def container_logs(name):
    r = sh(['docker', 'logs', name], check=False)
    text = redact(r.stdout + r.stderr)
    (logs / (name + '.log')).write_text(text)
    return text


def rpc(name, path, params, port='9000'):
    body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
    out = sh(['docker', 'exec', '-i', name, 'curl', '-s', '-c', '/tmp/jar', '-b', '/tmp/jar', '-H', 'Content-Type: application/json',
              '--data-binary', '@-', f'http://127.0.0.1:{port}{path}'], input=body).stdout
    return json.loads(out)


try:
    if sh(['docker', 'network', 'create', net], check=False).returncode:
        # Hosts with many networks exhaust Docker's default pools; pick a free private subnet instead.
        for _ in range(20):
            subnet = f'10.{secrets.randbelow(200) + 40}.{secrets.randbelow(250)}.0/24'
            if sh(['docker', 'network', 'create', '--subnet', subnet, net], check=False).returncode == 0:
                break
        else:
            raise RuntimeError('no free subnet for the test network')
    sh(['docker', 'volume', 'create', volume])
    module('1.0')

    # Odoo starts first and must wait for PostgreSQL.
    sh(['docker', 'create', '--name', pg, '--network', net, '-e', 'POSTGRES_USER=pgadmin',
        '-e', 'POSTGRES_PASSWORD=' + admin_password, args.postgres])
    containers.append(pg)
    first = tag + '-a'
    odoo(first, 'probe', '-e', 'INIT_LOAD_LANGUAGES=es_DO')
    time.sleep(8)
    sh(['docker', 'start', pg])
    result['first_boot_s'] = wait_healthy(first)
    log = container_logs(first)
    assert 'db_init:' in log and 'admin_password:' in log, 'first boot did not create the database'
    assert 'ADMIN_PASSWORD is not set' in log
    result['waited_for_postgres'] = True

    status = sh(['docker', 'exec', first, 'cat', '/proc/1/status']).stdout
    uid = next(line.split()[1] for line in status.splitlines() if line.startswith('Uid:'))
    assert uid == '1000', f'server runs as uid {uid}'
    owner = sh(['docker', 'exec', first, 'stat', '-c', '%u', '/srv/odoo/data/filestore/probe']).stdout.strip()
    assert owner == '1000', f'filestore owned by {owner}'
    result['dropped_root'] = True
    assert sh(['docker', 'exec', first, 'sh', '-c', 'command -v sudo'], check=False).returncode != 0
    result['no_sudo'] = True

    role = psql("SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = 'odoo_app'")
    assert role == 'f|t', role
    owner = psql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'probe'")
    assert owner == 'odoo_app', owner
    result['restricted_role'] = True
    assert 'DB_ADMIN_URL' not in sh(['docker', 'exec', '--privileged', '-u', '0', first, 'cat', '/proc/1/environ']).stdout
    result['admin_url_not_in_server_env'] = True

    auth = rpc(first, '/web/session/authenticate', {'db': 'probe', 'login': 'admin', 'password': odoo_password})
    assert auth.get('result', {}).get('uid'), redact(json.dumps(auth))[:500]
    result['admin_login'] = True
    listing = rpc(first, '/web/database/list', {})
    assert 'error' in listing, 'database listing is open'
    conf = sh(['docker', 'exec', first, 'cat', '/etc/odoo/odoo.conf']).stdout
    assert 'list_db = False' in conf and 'dbfilter = ^probe$' in conf and 'http_port = 9000' in conf
    assert 'my-weak-password' not in conf
    result['locked_manager'] = True
    installed = psql("SELECT string_agg(name, ',' ORDER BY name) FROM ir_module_module WHERE state = 'installed' AND name IN ('contacts','probe_hosted')", db='probe')
    assert installed == 'contacts,probe_hosted', installed
    language = psql("SELECT l.active, p.lang FROM res_lang l, res_users u JOIN res_partner p ON p.id = u.partner_id WHERE l.code = 'es_DO' AND u.login = 'admin'", db='probe')
    assert language == 't|es_DO', language
    result['language_loaded'] = True

    # A restart has nothing to do.
    sh(['docker', 'restart', first])
    result['restart_s'] = wait_healthy(first)
    tail = container_logs(first).rsplit('[dockerdoo] plan:', 1)[-1]
    assert 'install=[] upgrade=[]' in tail.splitlines()[0], tail.splitlines()[0]
    result['restart_noop'] = True

    # A newer manifest version is upgraded on the next start.
    module('1.1')
    sh(['docker', 'restart', first])
    wait_healthy(first)
    version = psql("SELECT latest_version FROM ir_module_module WHERE name = 'probe_hosted'", db='probe')
    assert version.endswith('.1.1'), version
    result['auto_upgrade'] = version

    # Two containers started together initialize one database once.
    second, third = tag + '-b', tag + '-c'
    odoo(second, 'probe_two', port='9001')
    odoo(third, 'probe_two', port='9002')
    wait_healthy(second)
    wait_healthy(third)
    inits = container_logs(second).count('db_init:') + container_logs(third).count('db_init:')
    assert inits == 1, f'{inits} initializations'
    result['concurrent_single_init'] = True

    # A non-root start keeps working on a volume it already owns.
    sh(['docker', 'rm', '-f', first])
    odoo(first + 'n', 'probe', root=False)
    wait_healthy(first + 'n')
    result['non_root_start'] = True
except Exception as error:
    result['error'] = redact(str(error))
finally:
    for name in containers:
        container_logs(name) if name != pg else None
    sh(['docker', 'rm', '-f', *containers], check=False)
    sh(['docker', 'volume', 'rm', '-f', volume], check=False)
    sh(['docker', 'network', 'rm', net], check=False)
    print(json.dumps(result, indent=2))
    print(f'Logs: {logs}')
    if 'error' in result:
        raise SystemExit(1)
