"""Validate and package the current service and fixed library; never publish or overwrite."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from release_checks import check, digest


def revision(root):
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()


def archive(repo, target, prefix):
    subprocess.run(['git', 'archive', '--format=tar.gz', f'--prefix={prefix}/',
                    f'--output={target.resolve()}', 'HEAD'], cwd=repo, check=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--library', type=Path, required=True)
    p.add_argument('--images', type=Path, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--validate-only', action='store_true')
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    accepted = check(root, args.library, args.images)
    accepted_path = root/'reports/1.1.0/acceptance.json'
    if args.validate_only:
        accepted_path.write_text(json.dumps(accepted, indent=2)+'\n')
        print('Engineering checks passed; customer accuracy and production deployment remain unaccepted.')
        return
    if not args.output:
        p.error('--output is required when packaging')
    if not accepted_path.exists() or json.loads(accepted_path.read_text()) != accepted:
        raise ValueError('Run --validate-only and commit acceptance before packaging')
    for repo in [root, args.library]:
        if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=repo):
            raise ValueError(f'Commit reviewed source and evidence first: {repo}')
    if args.output.exists():
        raise FileExistsError('Do not overwrite an existing release directory')
    args.output.mkdir(parents=True)
    for name, repo in [('imu-services', root), ('imu-inference', args.library)]:
        target = args.output/f'{name}-source.tar.gz'
        archive(repo, target, name)
        with tarfile.open(target) as tar:
            tar.extractall(args.output, filter='data')
    wheel = root/'vendor/wheels/imu_inference-1.0.0-py3-none-any.whl'
    shutil.copy2(wheel, args.output/wheel.name)
    sdist = args.library/'.release/imu_inference-1.0.0.tar.gz'
    repro = json.loads((args.library/'provenance/r3-wheel-reproducibility.json').read_text())
    if digest(sdist) != repro['source_distribution_sha256']:
        raise ValueError('Tested source distribution changed')
    shutil.copy2(sdist, args.output/sdist.name)
    exported = json.loads((root/'reports/1.1.0/image-export.json').read_text())
    shutil.copy2(args.images/exported['archive'], args.output/exported['archive'])
    (args.output/'README.md').write_text('''# IMU 1.1.0 离线交付

服务 1.1.0，固定推理库 1.0.0，HTTP v2，数据库 schema 2。

```bash
sha256sum -c SHA256SUMS
docker load -i imu-images-1.1.0.tar.gz
cd imu-services
python3 scripts/create_env.py
docker compose -f compose.yaml -f compose.offline.yaml up -d --wait
python3 scripts/smoke_http.py
```

交接入口：[服务说明](imu-services/README.md)、[接口](imu-services/docs/api.md)、[部署运维](imu-services/docs/operations.md)、[模型与验证](imu-inference/docs/model.md)。

测试与工程验收在 imu-services/reports/1.1.0。历史模型证据保留原版本。manifest.json 区分工程、客户准确率和生产部署状态。

两个展开源码目录与源码归档内容对应。wheel 保持已验收字节；配套 sdist 是重建同一 wheel 的已测试输入，最新交接文档在源码快照中。原始数据和完整训练工程不包含在内。
''')
    files = [dict(file=p.relative_to(args.output).as_posix(), bytes=p.stat().st_size, sha256=digest(p))
             for p in sorted(args.output.rglob('*')) if p.is_file()]
    manifest = dict(schema='imu.delivery.manifest.v2', status='ENGINEERING_VALIDATED',
                    customer_accuracy_accepted=False, production_deployed=False,
                    service_version='1.1.0', library_version='1.0.0', http_major=2, database_schema=2,
                    source_commits={'imu-services': revision(root), 'imu-inference': revision(args.library)},
                    acceptance=accepted, files=files)
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (args.output/'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.relative_to(args.output).as_posix()}\n'
        for p in sorted(args.output.rglob('*')) if p.is_file() and p != args.output/'SHA256SUMS'))
    print(f'Packaged {len(files)} files into {args.output}')


if __name__ == '__main__':
    main()
