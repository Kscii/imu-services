"""Evidence checks for the current fixed-wheel service release."""
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib
import xml.etree.ElementTree as ET
import zipfile


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def inventory(root, suffixes=None):
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'
            and (suffixes is None or p.suffix in suffixes)}


def junit(path):
    suites = list(ET.parse(path).getroot().iter('testsuite'))
    totals = {k: sum(int(s.get(k, 0)) for s in suites) for k in ['tests', 'errors', 'failures', 'skipped']}
    if not totals['tests'] or any(totals[k] for k in ['errors', 'failures', 'skipped']):
        raise ValueError(f'Tests incomplete or failed: {path.name}')
    return totals


def check(root, library, images):
    version = tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
    lib_version = tomllib.loads((library/'pyproject.toml').read_text())['project']['version']
    versions = json.loads((root/'release-versions.json').read_text())
    if (version, lib_version) != (versions['service_version'], versions['library_version']):
        raise ValueError('Project versions differ from the release contract')
    report_dir = root/'reports'/version
    names = ['tests.json', 'container-acceptance.json', 'image-consistency.json',
             'image-export.json', 'analysis-capacity.json', 'clean-install.json', 'release-identity.json']
    docs = {n: json.loads((report_dir/n).read_text()) for n in names}
    for n in set(names)-{'analysis-capacity.json', 'release-identity.json'}:
        if docs[n].get('passed') is not True:
            raise ValueError(f'Validation failed: {n}')
    cap = docs['analysis-capacity.json']
    if cap['status'] != 'PASS' or min(cap['segments'], cap['step_segments']) != 100000 or cap['dates'] != 31:
        raise ValueError('Maximum-bound analysis evidence missing')
    service = inventory(root/'src/imu_services', ('.py', '.sql'))
    if not service == docs['tests.json']['service_files'] == docs['image-consistency.json']['service_files']:
        raise ValueError('Service source differs from tested source or images')
    if digest(report_dir/'service-tests.xml') != docs['tests.json']['junit_sha256']:
        raise ValueError('Service test result changed')
    service_tests = junit(report_dir/'service-tests.xml')
    library_tests_path = library/f'provenance/{lib_version}/library-tests.xml'
    library_tests = junit(library_tests_path)
    wheel = root/f'vendor/wheels/imu_inference-{lib_version}-py3-none-any.whl'
    wheel_sha = digest(wheel)
    baseline = json.loads((root/'evidence/model-baseline-1.0.0/acceptance.json').read_text())
    build = json.loads((library/f'.release/{lib_version}/build-validation.json').read_text())
    if build['source_commit'] != subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=library, text=True).strip():
        raise ValueError('Library build is from a different source commit')
    if not build.get('passed') or not (wheel_sha == build['wheel_sha256'] ==
            build['sdist_rebuilt_wheel_sha256'] == docs['image-consistency.json']['wheel_sha256']):
        raise ValueError('Candidate wheel differs from its reproducible build or images')
    regression = json.loads((library/f'provenance/{lib_version}/patch-equivalence.json').read_text())
    if not regression.get('passed') or regression['baseline_wheel_sha256'] != baseline['wheel_sha256']:
        raise ValueError('Missing regression against the original frozen model')
    with zipfile.ZipFile(wheel) as z:
        contents = {n.removeprefix('imu_inference/'): hashlib.sha256(z.read(n)).hexdigest()
                    for n in z.namelist() if n.startswith('imu_inference/') and not n.endswith('/')}
    if contents != inventory(library/'src/imu_inference'):
        raise ValueError('Library source differs from the frozen wheel')
    if hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest() != \
            regression['candidate_inventory_sha256']:
        raise ValueError('Library source differs from the regression-tested candidate')
    consistency = docs['image-consistency.json']
    if consistency['source_commit'] != subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip():
        raise ValueError('Image source commit differs from this checkout')
    if consistency['service_version'] != version or consistency['library_version'] != lib_version:
        raise ValueError('Image versions differ')
    exported = docs['image-export.json']
    if exported['verification'] != consistency or not exported['all_layers_independently_rehashed']:
        raise ValueError('Image archive was not verified against these images')
    if digest(images/exported['archive']) != exported['sha256']:
        raise ValueError('Image archive changed')
    expected_refs = {f'localhost/imu-{t}:{version}' for t in ['runtime', 'query']}
    container = docs['container-acceptance.json']
    if {container['runtime_image'], container['query_image']} != expected_refs:
        raise ValueError('Container acceptance used another version')
    if container['image_ids'] != {i['reference']: i['engine_image_id'] for i in consistency['images']}:
        raise ValueError('Container acceptance used different image contents')
    for key in ['calendar_steps_match_direct_events', 'both_modes_calendar_api_equal',
                'worker_sigkill_recovery', 'whole_record_sigkill_recovery', 'query_database_role_readonly']:
        if container.get(key) is not True:
            raise ValueError(f'Missing container acceptance: {key}')
    installed = docs['clean-install.json']
    if (installed['imported_archive'] != exported['archive'] or not installed['new_database'] or
            not installed['environment_created_by_release_cli'] or
            installed['archive_sha256'] != exported['sha256']):
        raise ValueError('Clean offline installation was not validated')
    from imu_services.release import release_info
    if docs['release-identity.json'] != release_info():
        raise ValueError('Release identity does not match this environment')
    evidence = {n: digest(report_dir/n) for n in names+['service-tests.xml']}
    evidence['library-tests.xml'] = digest(library_tests_path)
    evidence['patch-equivalence.json'] = digest(library/f'provenance/{lib_version}/patch-equivalence.json')
    evidence['build-validation.json'] = digest(library/f'.release/{lib_version}/build-validation.json')
    evidence['source-license-review.json'] = digest(library/'provenance/source-license-review.json')
    return dict(schema='imu.delivery.acceptance.v2', engineering_passed=True,
                public_release_allowed=build['public_release_allowed'],
                customer_accuracy_accepted=False, production_deployed=False,
                service_version=version, library_version=lib_version, wheel_sha256=wheel_sha,
                service_tests=service_tests, library_tests=library_tests,
                evidence=evidence, inherited_model_evidence='evidence/model-baseline-1.0.0',
                limitations=['Research reference is not independent customer validation',
                             'Original 24/25-hour capacity belongs to the unchanged model and original host',
                             'This patch updates delivery and licensing; it does not improve model accuracy'])
