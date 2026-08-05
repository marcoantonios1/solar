import json

CONFIG_PATH = 'config.json'

with open(CONFIG_PATH) as f:
    config = json.load(f)

REQUIRED_SECTIONS = {'modbus', 'database', 'polling', 'thresholds', 'battery', 'location', 'edl_tariff'}
missing = REQUIRED_SECTIONS - config.keys()
if missing:
    raise SystemExit(f"config.json missing required sections: {missing}")