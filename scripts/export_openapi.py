"""Export request schemas without connecting to a database or loading models."""
import json
from pathlib import Path
from imu_services.api import create_app as intake
from imu_services.query import create_app as query
from imu_services.settings import Settings

root=Path(__file__).resolve().parents[1]
settings=Settings('postgresql://unused.invalid/unused', 'schema-export-placeholder')
for name, factory in [('intake', intake), ('query', query)]:
    (root/'docs'/f'openapi-{name}.json').write_text(json.dumps(factory(settings).openapi(), indent=2)+'\n')
