import json

CONFIG_PATH = 'config.json'

with open(CONFIG_PATH) as f:
    config = json.load(f)