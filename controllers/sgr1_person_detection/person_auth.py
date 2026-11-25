"""Zone lookup, authorization and alert cooldown. Pure Python; no Webots import.

Identity = the simulated tag (Solid 'model') reported by Webots Camera Recognition.
This is NOT facial recognition.
"""
import json
import math
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name('identities.json')
AUTHORIZED, UNAUTHORIZED, UNKNOWN = 'AUTHORIZED', 'UNAUTHORIZED', 'UNKNOWN'


def load_config(path=CONFIG_PATH):
    return json.loads(Path(path).read_text())


def zone_at(config, x, y):
    """Return zone code containing (x, y) world metres, or None (walls/doorways/outside)."""
    for code, zone in config['zones'].items():
        x0, x1, y0, y1 = zone['rect']
        if x0 <= x <= x1 and y0 <= y <= y1:
            return code
    return None


def classify(config, tag, zone):
    """UNKNOWN if the tag is unregistered or the zone cannot be resolved."""
    identity = config['identities'].get(tag)
    if identity is None or zone is None:
        return UNKNOWN
    return AUTHORIZED if zone in identity['allowed_zones'] else UNAUTHORIZED


def distance(relative_position):
    return math.sqrt(sum(v*v for v in relative_position))


class AlertGate:
    """At most one alert per identity per cooldown window (simulation seconds)."""
    def __init__(self, cooldown_s):
        self.cooldown_s = cooldown_s
        self.last = {}

    def allow(self, tag, now):
        previous = self.last.get(tag)
        if previous is not None and now - previous < self.cooldown_s:
            return False
        self.last[tag] = now
        return True


def alert_record(config, tag, zone, dist, now, position):
    identity = config['identities'].get(tag, {})
    return {'event': 'unauthorized_access', 'person': tag, 'badge_id': identity.get('badge_id'),
            'role': identity.get('role'), 'zone': zone, 'zone_name': config['zones'][zone]['name'],
            'distance_m': round(dist, 2), 'position_m': [round(v, 2) for v in position[:2]],
            'sim_time_s': round(now, 2), 'source': 'SG-R1 front_camera recognition (simulated identity)'}
