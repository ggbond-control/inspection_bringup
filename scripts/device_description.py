"""One merged device description for platform, TaskHub and cooperation adapters."""

import copy
import json
import math


def device_status(config):
    """Validate the merged system section and derive the platform identity once."""
    source = config.get('device_status', {})
    if not isinstance(source, dict):
        raise ValueError('device_status must be a mapping')
    status = copy.deepcopy(source)
    if 'sn' in status:
        raise ValueError('device_status.sn is derived from robot_id / mqtt.sn; remove the duplicate')
    status['sn'] = config.get('mqtt', {}).get('sn', config.get('cooperation', {}).get('robot_id', ''))
    capabilities = status.setdefault('capabilities', [])
    if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item.strip() or item != item.strip()
            for item in capabilities):
        raise ValueError('device_status.capabilities must be an array of nonempty capability strings')
    speed = status.setdefault('nominal_speed_mps', 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
        raise ValueError('device_status.nominal_speed_mps must be positive and finite')
    capacity, unit = status.get('Battery_RC'), status.get('Battery_RC_unit')
    if capacity is not None or unit is not None:
        if (isinstance(capacity, bool) or not isinstance(capacity, (int, float))
                or not math.isfinite(capacity) or capacity <= 0):
            raise ValueError('device_status.Battery_RC must be positive and finite')
        if unit not in ('Ah', 'Wh'):
            raise ValueError('device_status.Battery_RC_unit must be Ah or Wh')
    else:
        # Explicit nulls can clear an inherited battery specification.
        status.pop('Battery_RC', None)
        status.pop('Battery_RC_unit', None)

    def validate_devices(devices):
        if not isinstance(devices, list):
            raise ValueError('device_status.subDevices must be an array')
        identifiers = set()
        for item in devices:
            if not isinstance(item, dict):
                raise ValueError('device_status.subDevices entries must be mappings')
            identifier = item.get('identifier')
            if not isinstance(identifier, str) or not identifier.strip() or identifier != identifier.strip():
                raise ValueError('device_status.subDevices.identifier must be a nonempty string')
            if identifier in identifiers:
                raise ValueError('device_status.subDevices contains duplicate identifier: ' + identifier)
            identifiers.add(identifier)
            if 'supportStream' in item and not isinstance(item['supportStream'], bool):
                raise ValueError('device_status.subDevices.supportStream must be a boolean')
            if 'subDevices' in item:
                validate_devices(item['subDevices'])

    validate_devices(status.setdefault('subDevices', []))
    try:
        json.dumps(status, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('device_status must contain finite JSON-compatible values') from exc
    return status


def device_status_snapshot(config):
    """Adapt file-based consumers without another user-maintained JSON file."""
    from bringup_config import parameter_snapshot

    return parameter_snapshot(device_status(config), '.json')
