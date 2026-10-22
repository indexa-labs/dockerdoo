"""Run the image the way hosted platforms do and check the result.

Root start on a root-owned volume, a platform PORT, Odoo before PostgreSQL, a superuser-only
PostgreSQL, automatic database creation and upgrade, a database loaded from a dump, JSON logs on
stdout, restart, two containers starting together, and, with --native-tests, Odoo's own base tests.
"""

import argparse
import json
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', required=True)
parser.add_argument('--native-tests', action='store_true')
parser.add_argument('--development', action='store_true', help="check the development image's tools instead of their absence")
args = parser.parse_args()
image = args.image

tag = 'dkhosted-' + secrets.token_hex(3)
net, pg, volume = tag + '-net', tag + '-pg', tag + '-data'
admin_pw, app_pw, odoo_pw = (secrets.token_hex(12) for _ in range(3))
addons = Path(tempfile.mkdtemp())
names = []


def sh(*cmd, check=True, input=None):
    r = subprocess.run(cmd, text=True, capture_output=True, input=input, timeout=900)
    if check and r.returncode:
        raise RuntimeError(f'{cmd[:3]}: {(r.stdout + r.stderr)[-2000:]}')
    return r.stdout.strip()


def sql(query, db='postgres'):
    return sh('docker', 'exec', '-e', 'PGPASSWORD=' + admin_pw, pg, 'psql', '-XAt', '-h', '127.0.0.1', '-U', 'pgadmin', '-d', db, '-c', query)


def module(version, name='probe_hosted'):
    root = addons / 'probe' / name
    root.mkdir(parents=True, exist_ok=True)
    # A later version adds a stored field, which the upgrade must create before Odoo reads partners.
    (root / '__init__.py').write_text('' if version == '1.0' else 'from odoo import fields, models\n\n\n'
                                      'class ResPartner(models.Model):\n    _inherit = "res.partner"\n    probe_hosted = fields.Char()\n')
    (root / '__manifest__.py').write_text(repr({'name': 'Probe', 'version': version, 'depends': ['base'], 'license': 'LGPL-3'}))
    # The image's odoo user rarely shares the runner's uid.
    for path in (addons, root.parent, root):
        path.chmod(0o755)


def start(name, dbname, root=True, **env):
    env = {'INIT_ADMIN_PASSWORD': odoo_pw, **env}
    sh('docker', 'run', '-d', '--name', name, '--network', net, *([] if root else ['--user', '1000']),
       '-v', volume + ':/srv/odoo', '-v', f'{addons}:/mnt/extra-addons:ro', '--health-interval', '2s',
       '-e', 'ODOO_DATA_DIR=/srv/odoo/data', '-e', 'PORT=9000', '-e', 'ODOO_EXTRA_OPTIONS=probe_option = 42',
       '-e', 'PGHOST=' + pg, '-e', 'PGUSER=odoo_app', '-e', 'PGPASSWORD=' + app_pw,
       '-e', f'DB_ADMIN_URL=postgresql://pgadmin:{admin_pw}@{pg}/postgres', '-e', 'PGDATABASE=' + dbname,
       '-e', 'AUTO_INIT=1', '-e', 'AUTO_UPGRADE=1', '-e', 'INIT_MODULES=contacts,probe_hosted',
       *(arg for k, v in env.items() for arg in ('-e', f'{k}={v}')), image)
    names.append(name)


def healthy(name):
    for _ in range(300):
        state = sh('docker', 'inspect', '-f', '{{.State.Status}} {{.State.Health.Status}}', name)
        if state == 'running healthy':
            return
        if not state.startswith('running'):
            break
        time.sleep(2)
    raise RuntimeError(f'{name} is not healthy: ' + subprocess.run(['docker', 'logs', name], capture_output=True, text=True).stderr[-3000:])


def created(name, log='Create database'):
    return subprocess.run(['docker', 'logs', name], capture_output=True, text=True).stderr.count(log)


try:
    # Hosts with many networks exhaust Docker's default pools; fall back to an explicit subnet.
    if subprocess.run(['docker', 'network', 'create', net], capture_output=True).returncode:
        sh('docker', 'network', 'create', '--subnet', f'10.{secrets.randbelow(200) + 40}.{secrets.randbelow(250)}.0/24', net)
    sh('docker', 'volume', 'create', volume)
    module('1.0')
    sh('docker', 'create', '--name', pg, '--network', net, '-e', 'POSTGRES_USER=pgadmin', '-e', 'POSTGRES_PASSWORD=' + admin_pw, 'postgres:16')
    names.append(pg)
    first = tag + '-a'
    start(first, 'probe')
    time.sleep(3)  # Odoo is now waiting for a PostgreSQL that does not exist yet.
    sh('docker', 'start', pg)
    healthy(first)
    assert created(first) == 1

    assert sh('docker', 'exec', first, 'awk', '/^Uid:/{print $2}', '/proc/1/status') == '1000'
    assert sh('docker', 'exec', first, 'stat', '-c', '%u', '/srv/odoo/data/filestore/probe') == '1000'
    if args.development:
        # The development image adds the tools production leaves out, and the odoo user may use sudo.
        for tool in ('git', 'npm', 'ffmpeg', 'gcc'):
            sh('docker', 'exec', first, 'which', tool)
        sh('docker', 'exec', first, 'python3', '-c', 'import debugpy, watchdog')
        sh('docker', 'exec', '-u', 'odoo', first, 'sudo', '-n', 'true')
    else:
        assert subprocess.run(['docker', 'exec', first, 'which', 'sudo']).returncode != 0
    # Odoo's thread pool and memory limits are fitted to the container's memory, unless the environment sets them.
    def derived(*docker_args, **env):
        out = sh('docker', 'run', '--rm', *docker_args, *(arg for k, v in env.items() for arg in ('-e', f'{k}={v}')), image, 'env')
        return dict(line.split('=', 1) for line in out.splitlines() if line.startswith(('ODOO_MAX_HTTP', 'ODOO_LIMIT_MEMORY', 'ODOO_WORKERS')))
    small = derived('--memory=512m')
    assert small['ODOO_MAX_HTTP_THREADS'] == '32', small
    # Odoo measures virtual memory, which includes each thread's 8 MiB stack.
    assert int(small['ODOO_LIMIT_MEMORY_SOFT']) == int(512 * 2**20 * 0.70) + 32 * 8 * 2**20 and int(small['ODOO_LIMIT_MEMORY_HARD']) > int(small['ODOO_LIMIT_MEMORY_SOFT'])
    assert derived('--memory=2g')['ODOO_MAX_HTTP_THREADS'] == '128'
    explicit = derived('--memory=512m', ODOO_MAX_HTTP_THREADS='7', ODOO_LIMIT_MEMORY_HARD='123456789')
    assert explicit['ODOO_MAX_HTTP_THREADS'] == '7' and explicit['ODOO_LIMIT_MEMORY_HARD'] == '123456789'
    assert explicit['ODOO_LIMIT_MEMORY_SOFT'] == str(int(512 * 2**20 * 0.70) + 7 * 8 * 2**20)
    # Worker processes have limits of their own.
    assert 'ODOO_LIMIT_MEMORY_SOFT' not in derived('--memory=512m', ODOO_WORKERS='2')
    # The slim image keeps the shared libraries and tools Odoo's Python packages and reports load.
    assert sh('docker', 'exec', first, 'bash', '-c', "find /usr/local/lib/python3.12/site-packages -name '*.so*' -not -path '*.libs/*' -exec ldd {} + | grep 'not found'; true") == ''
    sh('docker', 'exec', first, 'python3', '-c', 'import ldap, cairo, rlPyCairo, magic, psycopg2, lxml, PIL, reportlab.graphics.renderPM')
    for tool in ('rtlcss', 'lessc', 'wkhtmltopdf', 'pg_dump', 'psql'):
        sh('docker', 'exec', first, tool, '--version')
    assert sh('docker', 'exec', first, 'bash', '-c', "echo '<h1>hola</h1>' | wkhtmltopdf -q - - | head -c 4") == '%PDF'
    assert 'DB_ADMIN_URL' not in sh('docker', 'exec', '--privileged', '-u', '0', first, 'cat', '/proc/1/environ')
    assert sql("SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = 'odoo_app'") == 'f|t'
    assert sql("SELECT count(*) FROM ir_module_module WHERE state = 'installed' AND name IN ('contacts', 'probe_hosted')", 'probe') == '2'

    def rpc(path, params, name=first):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        return json.loads(sh('docker', 'exec', '-i', name, 'curl', '-s', '-H', 'Content-Type: application/json',
                             '--data-binary', '@-', 'http://127.0.0.1:9000' + path, input=body))
    assert rpc('/web/session/authenticate', {'db': 'probe', 'login': 'admin', 'password': odoo_pw})['result']['uid']
    # Platforms reach the server from outside the container.
    assert '"pass"' in sh('docker', 'run', '--rm', '--network', net, '--entrypoint', 'curl', image, '-s', f'http://{first}:9000/web/health')
    assert 'error' in rpc('/web/database/list', {})
    assert sh('docker', 'exec', '-i', first, 'odoo', 'shell', '-d', 'probe', '--no-http',
              input="from odoo.tools import config; print('probe_option=' + config['probe_option'])").count('probe_option=42') == 1

    # Hosts that read structured logs get one JSON object per record on stdout and nothing on stderr, and Odoo's level options still apply.
    probe = "import logging\nlog = logging.getLogger('odoo.probe')\nlog.info('hello')\nlog.warning('careful')\ntry:\n    1 / 0\nexcept ZeroDivisionError:\n    log.exception('broken')\n"

    def json_log(**env):
        r = subprocess.run(['docker', 'exec', '-i', '-e', 'ODOO_LOG_CONFIG=/dockerdoo_logging.json', *(arg for k, v in env.items() for arg in ('-e', f'{k}={v}')),
                            first, 'odoo', 'shell', '-d', 'probe', '--no-http'], input=probe, capture_output=True, text=True)
        assert r.returncode == 0 and not r.stderr, r.stderr
        return [json.loads(line) for line in r.stdout.splitlines()]
    records = [r for r in json_log() if r['logger'] == 'odoo.probe']
    assert [r['level'] for r in records] == ['info', 'warn', 'error']
    assert 'Traceback' in records[-1]['message'] and 'ZeroDivisionError' in records[-1]['message']
    assert {r['level'] for r in json_log(ODOO_LOG_LEVEL='warn')} == {'warn', 'error'}

    # pg_dump must be as new as the server, or Odoo's dump and backup fail.
    sh('docker', 'exec', first, 'odoo', 'db', 'dump', 'probe', '/srv/odoo/data/probe.zip')

    # A database loaded from the dump takes its administrator from the environment, and a later start keeps it.
    loaded = tag + '-d'
    owner = {'INIT_DUMP': '/srv/odoo/data/probe.zip', 'INIT_ADMIN_LOGIN': 'owner', 'INIT_ADMIN_PASSWORD': secrets.token_hex(12)}
    start(loaded, 'probe_dump', **owner)
    healthy(loaded)
    assert created(loaded, 'RESTORING DB') == 1
    credentials = {'db': 'probe_dump', 'password': owner['INIT_ADMIN_PASSWORD']}
    assert rpc('/web/session/authenticate', {**credentials, 'login': 'owner'}, loaded)['result']['uid']
    assert 'error' in rpc('/web/session/authenticate', {**credentials, 'login': 'admin'}, loaded)
    sh('docker', 'rm', '-f', loaded)
    start(loaded + 'r', 'probe_dump', **owner)
    healthy(loaded + 'r')
    assert created(loaded + 'r', 'RESTORING DB') == 0

    # A later start without root, with a newer manifest, upgrades the module and creates nothing.
    sh('docker', 'rm', '-f', first)
    module('1.1')
    start(first + 'n', 'probe', root=False)
    healthy(first + 'n')
    assert created(first + 'n') == 0
    assert sql("SELECT latest_version FROM ir_module_module WHERE name = 'probe_hosted'", 'probe') == '20.0.1.1'
    assert sql("SELECT count(*) FROM information_schema.columns WHERE table_name = 'res_partner' AND column_name = 'probe_hosted'", 'probe') == '1'

    # A start with no outdated module still lists add-ons new to the image.
    module('1.0', 'probe_listed')
    sh('docker', 'rm', '-f', first + 'n')
    start(first + 'r', 'probe')
    # Two containers starting together create one database.
    start(tag + '-b', 'probe_two')
    start(tag + '-c', 'probe_two')
    healthy(first + 'r')
    assert sql("SELECT state FROM ir_module_module WHERE name = 'probe_listed'", 'probe') == 'uninstalled'
    healthy(tag + '-b')
    healthy(tag + '-c')
    assert created(tag + '-b') + created(tag + '-c') == 1
    print('hosted checks passed', flush=True)

    if args.native_tests:
        # The exclusions are timing- or environment-sensitive tests that fail in containers regardless of the image.
        skip = ['base:TestPerformance.test_frequencies_1ms_sleep', 'base:TestSyncRecorder.test_sync_recorder',
                'test_retry_failures', 'test_retry_disable', 'base:TestRunnerLogging.test_assertQueryCount',
                'base:TestExpression.test_invalid', 'base:TestUsersIdentitycheck.test_revoke_all_devices']
        # The image's health probe would show up as a stray process in Odoo's test teardown checks.
        run = subprocess.run(['docker', 'run', '--rm', '--no-healthcheck', '--network', net, '-e', 'PGHOST=' + pg, '-e', 'PGUSER=odoo_app',
                              '-e', 'PGPASSWORD=' + app_pw, image, 'odoo', 'server', '-d', 'native_tests', '-i', 'base',
                              '--test-enable', '--stop-after-init', '--no-http', '--test-tags', ','.join(['base', *('-' + s for s in skip)])],
                             capture_output=True, text=True, timeout=3600)
        summary = re.findall(r'odoo\.tests\.result: (\d+) failed, (\d+) error\(s\) of (\d+) tests', run.stderr)
        assert run.returncode == 0 and summary and summary[-1][:2] == ('0', '0') and int(summary[-1][2]) > 0, run.stderr[-3000:]
        print(f'native tests passed: {summary[-1][2]}')
finally:
    subprocess.run(['docker', 'rm', '-f', *names], capture_output=True)
    subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
    subprocess.run(['docker', 'network', 'rm', net], capture_output=True)
