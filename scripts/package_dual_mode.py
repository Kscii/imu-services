"""Package frozen source, verified images and separately bound validation evidence."""
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library', type=Path, required=True)
    p.add_argument('--images', type=Path, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--validate-only', action='store_true')
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    accepted = check(root, args.library, args.images)
    version, lib_version = accepted['service_version'], accepted['library_version']
    reports = root/'reports'/version
    accepted_path = reports/'acceptance.json'
    if args.validate_only:
        accepted_path.write_text(json.dumps(accepted, indent=2)+'\n')
        print('Engineering checks passed; customer accuracy and production deployment remain unaccepted.')
        return
    if not args.output:
        p.error('--output is required when packaging')
    if not accepted_path.exists() or json.loads(accepted_path.read_text()) != accepted:
        raise ValueError('Run --validate-only for this exact source and evidence first')
    for repo in [root, args.library]:
        if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=repo):
            raise ValueError(f'Commit reviewed source before packaging: {repo}')
    if args.output.exists():
        raise FileExistsError('Do not overwrite an existing release directory')
    args.output.mkdir(parents=True)
    for name, repo in [('imu-services', root), ('imu-inference', args.library)]:
        target = args.output/f'{name}-source.tar.gz'
        archive(repo, target, name)
        with tarfile.open(target) as tar:
            tar.extractall(args.output, filter='data')
    wheel = root/f'vendor/wheels/imu_inference-{lib_version}-py3-none-any.whl'
    shutil.copy2(wheel, args.output/wheel.name)
    build = json.loads((args.library/f'.release/{lib_version}/build-validation.json').read_text())
    sdist = args.library/f'.release/{lib_version}/imu_inference-{lib_version}.tar.gz'
    if digest(sdist) != build['sdist_sha256']:
        raise ValueError('Tested source distribution changed')
    shutil.copy2(sdist, args.output/sdist.name)
    exported = json.loads((reports/'image-export.json').read_text())
    shutil.copy2(args.images/exported['archive'], args.output/exported['archive'])
    validation = args.output/'validation'
    shutil.copytree(reports, validation/f'imu-services-{version}')
    shutil.copytree(args.library/f'provenance/{lib_version}', validation/f'imu-inference-{lib_version}')
    shutil.copy2(args.library/f'.release/{lib_version}/build-validation.json',
                 validation/f'imu-inference-{lib_version}/build-validation.json')
    (args.output/'README.md').write_text(f'''# IMU {version} 完整离线交付

服务 {version}，固定推理库 {lib_version}，HTTP v2，数据库 schema 2。
Linux x86_64 CPU；Docker Engine、Compose，主机 Python 3.10+；默认建议至少 16 GiB 可用内存并为宿主机留余量。

```bash
set -e
sha256sum -c SHA256SUMS
docker load -i imu-images-{version}.tar.gz
cd imu-services
python3 scripts/create_env.py --mode offline
docker compose -f compose.yaml -f compose.offline.yaml up -d --wait --wait-timeout 300
python3 scripts/smoke_http.py
```

交接入口：[服务](imu-services/README.md)、[接口](imu-services/docs/api.md)、[运维](imu-services/docs/operations.md)、[模型手册](imu-inference/docs/model.md)、[模型许可](imu-inference/MODEL_LICENSES.md)。

新版本验证在 validation/；历史报告保持原路径及日期。manifest.json 区分工程验证、客户准确率和生产部署状态。合成 150 秒样例含 1 秒缺失，预期 149 秒有效观测，不能证明识别准确率。

展开源码与相应源码归档一致。验证产物独立绑定提交、wheel、镜像与测试结果，避免把构建自身摘要写回源提交形成循环依赖。原始数据和完整重训工程不包含在内。新包摘要与旧包不同，原模型及计算输出通过单独回归核对。
''')
    files = [dict(file=p.relative_to(args.output).as_posix(), bytes=p.stat().st_size, sha256=digest(p))
             for p in sorted(args.output.rglob('*')) if p.is_file()]
    manifest = dict(schema='imu.delivery.manifest.v2', status='ENGINEERING_VALIDATED',
                    customer_accuracy_accepted=False, production_deployed=False,
                    service_version=version, library_version=lib_version, http_major=2, database_schema=2,
                    source_commits={'imu-services': revision(root), 'imu-inference': revision(args.library)},
                    acceptance=accepted, files=files)
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (args.output/'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.relative_to(args.output).as_posix()}\n'
        for p in sorted(args.output.rglob('*')) if p.is_file() and p != args.output/'SHA256SUMS'))
    print(f'Packaged {len(files)} files into {args.output}')


if __name__ == '__main__':
    main()
