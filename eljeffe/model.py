"""Schema/property/reference checks using an attributed upstream FtM model.

Value normalization remains the upstream followthemoney library's responsibility;
this module never silently changes or drops imported source facts.
"""

from functools import lru_cache
from importlib.resources import files
import json


@lru_cache(maxsize=1)
def model():
    return json.loads(files("eljeffe").joinpath("ftm_model.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=256)
def properties(schema):
    definition = model()["schemata"].get(schema)
    if definition is None:
        return {}
    result = {}
    for parent in definition.get("extends", []):
        result.update(properties(parent))
    result.update(definition.get("properties", {}))
    return result


def references(entity):
    definitions = properties(entity["schema"])
    for name, values in entity["properties"].items():
        if definitions.get(name, {}).get("type") == "entity":
            for value in values:
                yield name, value


def schema_errors(entity):
    if entity.get("schema") not in model()["schemata"]:
        return [f"Unknown schema {entity.get('schema')!r} in bundled FtM {model()['version']}"]
    definitions = properties(entity["schema"])
    return [f"Unknown property {entity['schema']}.{name}" for name in entity["properties"]
            if name not in definitions]
