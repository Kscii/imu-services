"""Run the whole service suite against a disposable local PostgreSQL container."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def source_files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.suffix in ('.py', '.sql')}


def main():
    root = Path(__file__).resolve().parents[1]
    output = root/'reports/1.1.0'
    output.mkdir(parents=True, exist_ok=True)
    name = 'imu-delivery-tests-' + secrets.token_hex(5)
    subprocess.run([sys.executable, '-m', 'ruff', 'check', '.'], cwd=root, check=True)
    with tempfile.TemporaryDirectory(prefix='imu-tests-') as tmp:
        secret = secrets.token_hex(24)
        envfile = Path(tmp)/'postgres.env'
        envfile.write_text('POSTGRES_USER=imu_test\nPOSTGRES_DB=imu_test\nPOSTGRES_PASSWORD='+secret+'\n')
        envfile.chmod(0o600)
        try:
            subprocess.run(['docker', 'run', '-d', '--name', name, '--env-file', str(envfile),
                            '-p', '127.0.0.1::5432', 'localhost/imu-postgres:18-20261020'],
                           check=True, stdout=subprocess.DEVNULL)
            for _ in range(60):
                ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'imu_test'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('isolated PostgreSQL failed to start')
            port = subprocess.check_output(['docker', 'port', name, '5432'], text=True).strip().rsplit(':', 1)[1]
            env = dict(os.environ, IMU_TEST_DATABASE_URL=f'postgresql://imu_test:{secret}@127.0.0.1:{port}/imu_test')
            subprocess.run([sys.executable, '-m', 'pytest', '-q', '--junitxml='+str(output/'service-tests.xml')],
                           cwd=root, env=env, check=True)
            tree = ET.parse(output/'service-tests.xml')
            suites = list(tree.getroot().iter('testsuite'))
            counts = {k: sum(int(s.attrib.get(k, 0)) for s in suites)
                      for k in ['tests', 'failures', 'errors', 'skipped']}
            if not counts['tests'] or any(counts[k] for k in ['failures', 'errors', 'skipped']):
                raise RuntimeError('all service tests must run and pass without skips')
            report = dict(passed=True, service_version='1.1.0', **counts,
                          service_files=source_files(root/'src/imu_services'),
                          junit_sha256=hashlib.sha256((output/'service-tests.xml').read_bytes()).hexdigest())
            (output/'tests.json').write_text(json.dumps(report, indent=2)+'\n')
        finally:
            subprocess.run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
