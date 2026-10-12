"""Turning a measured right-sizing proposal into an edit of platform.yaml that keeps the file's comments and layout."""
import re

import yaml

# Report setting names -> (key in platform.yaml, parent block); every key name is unique in the schema.
SETTINGS = {"resources.memory_mib": "memory_mib", "resources.cpu": "cpu", "concurrency": "concurrency", "scaling.minimum": "minimum"}
PATHS = {"resources.memory_mib": ("resources", "memory_mib"), "resources.cpu": ("resources", "cpu"), "concurrency": ("concurrency",), "scaling.minimum": ("scaling", "minimum")}


def apply_settings(text, changes):
    """Rewrite only the changed values in place; when a key is absent the file is regenerated from the model instead."""
    applied, missing = [], []
    lines = text.splitlines(keepends=True)
    for setting, value in changes.items():
        key = SETTINGS[setting]
        pattern = re.compile(rf"^(?P<lead>\s*{key}:\s*)(?P<value>[^#\n]*?)(?P<tail>\s*(#.*)?)$")
        for index, line in enumerate(lines):
            match = pattern.match(line.rstrip("\n"))
            if match:
                lines[index] = f"{match['lead']}{value}{match['tail']}\n"
                applied.append({"setting": setting, "value": value})
                break
        else:
            missing.append(setting)
    if not missing:
        return "".join(lines), applied, False
    data = yaml.safe_load(text) or {}
    for setting, value in changes.items():
        node = data
        for part in PATHS[setting][:-1]:
            node = node.setdefault(part, {})
        node[PATHS[setting][-1]] = value
        if setting in missing:
            applied.append({"setting": setting, "value": value})
    return yaml.safe_dump(data, sort_keys=False), applied, True
