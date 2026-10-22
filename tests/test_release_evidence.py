"""Release evidence must never turn skipped or failing tests into acceptance."""
from pathlib import Path
import runpy
import pytest

junit = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/release_checks.py'))['junit']


@pytest.mark.parametrize('attribute,value', [('tests', 0), ('skipped', 1), ('failures', 1), ('errors', 1)])
def test_junit_rejects_incomplete_acceptance(tmp_path, attribute, value):
    attrs = dict(tests=3, failures=0, errors=0, skipped=0)
    attrs[attribute] = value
    p = tmp_path/'results.xml'
    p.write_text('<testsuites><testsuite '+ ' '.join(f'{k}="{v}"' for k,v in attrs.items())+'/></testsuites>')
    with pytest.raises(ValueError):
        junit(p)


def test_junit_aggregates_all_suites(tmp_path):
    p=tmp_path/'results.xml'
    p.write_text('<testsuites><testsuite tests="3"/><testsuite tests="4"/></testsuites>')
    assert junit(p) == dict(tests=7, failures=0, errors=0, skipped=0)
