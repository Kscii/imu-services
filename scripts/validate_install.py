"""Verify the offline entry point in an isolated Compose project with an empty database."""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from container_acceptance import free_port
from release_checks import digest


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--output', type=Path, default=Path('reports/1.1.1/clean-install.json'))
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    subprocess.run(['docker', 'load', '-i', str(args.archive.resolve())], check=True)
    with tempfile.TemporaryDirectory(prefix='imu-install-') as tmp:
        install_root = Path(tmp)
        (install_root/'scripts').mkdir()
        for relative in ('.env.example', 'release-versions.json', 'scripts/create_env.py'):
            shutil.copy2(root/relative, install_root/relative)
        subprocess.run([sys.executable, str(install_root/'scripts/create_env.py'), '--mode', 'offline'],
                       check=True)
        env = Path(tmp)/'.env'
        config = env.read_text()
        config = config.replace('IMU_API_PORT=8100', f'IMU_API_PORT={free_port()}')
        config = config.replace('IMU_QUERY_PORT=8101', f'IMU_QUERY_PORT={free_port()}')
        config = config.replace('IMU_WORKERS=2', 'IMU_WORKERS=1')
        env.write_text(config)
        env.chmod(0o600)
        project = 'imu-clean-install-'+secrets.token_hex(4)
        cmd = ['docker', 'compose', '--env-file', str(env), '-p', project, '-f', str(root/'compose.yaml'),
               '-f', str(root/'compose.offline.yaml')]
        try:
            subprocess.run(cmd+['up', '-d', '--wait', '--wait-timeout', '300'], check=True)
            result = Path(tmp)/'smoke.json'
            subprocess.run([sys.executable, str(root/'scripts/smoke_http.py'), '--env-file', str(env),
                            '--output', str(result)], check=True)
            smoke = json.loads(result.read_text())
            assert smoke['passed'] and set(smoke['results']) == {'incremental', 'whole_record'}
            report = dict(passed=True, imported_archive=args.archive.name,
                          archive_sha256=digest(args.archive),
                          environment_created_by_release_cli=True,
                          new_database=True, offline_pull_policy_never=True, synthetic=True,
                          accuracy_evidence=False, smoke=smoke)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2)+'\n')
        finally:
            subprocess.run(cmd+['down', '--volumes', '--remove-orphans'], check=True)


if __name__ == '__main__':
    main()
