import json
import ssl
import websocket
import config
import difflib
import requests
import re
from typing import List, Dict, Set, Tuple, Optional, Any, Union

# Determine the protocol based on TLS configuration
TLS_S = "s" if config.TLS else ""


def connect_websocket() -> Optional[websocket.WebSocket]:
    """
    Establishes a WebSocket connection to Home Assistant and authenticates.
    Returns the websocket object if successful, None otherwise.
    """
    websocket_url = f"ws{TLS_S}://{config.HOST}/api/websocket"
    sslopt = {"cert_reqs": ssl.CERT_NONE} if not config.SSL_VERIFY else {}
    ws = websocket.WebSocket(sslopt=sslopt)
    try:
        ws.connect(websocket_url)
    except Exception as e:
        print(f"Failed to connect to {websocket_url}: {e}")
        return None

    auth_req = ws.recv()

    # Authenticate with Home Assistant
    auth_msg = json.dumps({"type": "auth", "access_token": config.ACCESS_TOKEN})
    ws.send(auth_msg)
    auth_result = ws.recv()
    auth_result = json.loads(auth_result)
    if auth_result["type"] != "auth_ok":
        print("Authentication failed. Check your access token.")
        ws.close()
        return None
    return ws


def align_strings(table: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
    """
    Aligns columns in a table by splitting strings at a delimiter ('.').
    Used for aligning entity IDs like 'domain.name'.
    """
    alignment_char = "."

    if len(table) == 0:
        return table

    for column in range(len(table[0])):
        # Get the column data from the table
        column_data = [row[column] for row in table]

        # Find the maximum length of the first part of the split strings
        strings_to_align = [
            s for s in column_data if isinstance(s, str) and alignment_char in s
        ]
        if len(strings_to_align) == 0:
            continue

        max_length = max([len(s.split(alignment_char)[0]) for s in strings_to_align])

        def align_string(s: Any) -> Any:
            if not isinstance(s, str):
                return s
            s_split = s.split(alignment_char, maxsplit=1)
            if len(s_split) == 1:
                return s
            else:
                return f"{s_split[0]:>{max_length}}.{s_split[1]}"

        # Create the modified table by replacing the column with aligned strings
        table = [
            tuple(
                align_string(value) if i == column else value
                for i, value in enumerate(row)
            )
            for row in table
        ]

    return table


def get_valid_entities(ws: websocket.WebSocket, msg_id: int) -> Tuple[Set[str], int]:
    """
    Fetches all valid entity IDs from the Entity Registry and the State Machine.
    Returns a set of entity IDs and the updated msg_id.
    """
    # Get registry entities
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "config/entity_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    entities = set()
    if result["success"]:
        entities = {e["entity_id"] for e in result["result"]}
    else:
        print("Failed to list registry entities.")

    # Get state entities (includes non-registry items like zone.home, sun.sun)
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "get_states"}))
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        for e in result["result"]:
            entities.add(e["entity_id"])
    else:
        print("Failed to list states.")

    return entities, msg_id


def suggest_fix(broken_ref: str, valid_entities: Set[str]) -> List[str]:
    """
    Suggests potential fixes for a broken entity reference using fuzzy matching
    and common suffix removal.
    """
    if "." not in broken_ref:
        return []

    domain, name = broken_ref.split(".", 1)
    suggestions = []

    # 1. Fuzzy matching using difflib
    # Filter valid entities to only those in the same domain to improve accuracy
    same_domain_entities = [e for e in valid_entities if e.startswith(f"{domain}.")]

    matches = difflib.get_close_matches(
        broken_ref, same_domain_entities, n=3, cutoff=0.6
    )
    suggestions.extend(matches)

    # 2. Common suffixes that might have been removed during a reset
    suffixes = [
        "_switch",
        "_light",
        "_sensor",
        "_binary_sensor",
        "_cover",
        "_fan",
        "_lock",
        "_climate",
        "_media_player",
    ]

    # Try removing suffixes
    for suffix in suffixes:
        if name.endswith(suffix):
            new_name = name[: -len(suffix)]
            candidate = f"{domain}.{new_name}"
            if candidate in valid_entities:
                suggestions.append(candidate)

    # Deduplicate while preserving order
    seen = set()
    unique_suggestions = []
    for s in suggestions:
        if s not in seen:
            unique_suggestions.append(s)
            seen.add(s)

    return unique_suggestions


def save_automation_config(automation_config: Dict[str, Any]) -> bool:
    """
    Saves an automation configuration to Home Assistant via the HTTP API.
    """
    automation_id = automation_config.get("id")
    if not automation_id:
        print("Error: Automation config missing ID.")
        return False

    url = f"http{TLS_S}://{config.HOST}/api/config/automation/config/{automation_id}"
    headers = {
        "Authorization": f"Bearer {config.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url, headers=headers, json=automation_config, verify=config.SSL_VERIFY
        )
        if response.status_code == 200:
            return True
        else:
            print(f"Failed to save automation {automation_id}: {response.text}")
            return False
    except Exception as e:
        print(f"Exception while saving automation {automation_id}: {e}")
        return False


def replace_references(data: Union[Dict, List], old_ref: str, new_ref: str) -> bool:
    """
    Recursively replace references in a config object (dict or list).
    Handles exact matches and substrings (e.g. in templates) using word boundaries.
    Returns True if any modification was made.
    """
    modified = False

    # Regex for safe replacement: old_ref followed by non-identifier char or end of string
    # We assume old_ref is a valid entity_id (domain.name).
    pattern = re.compile(re.escape(old_ref) + r"(?![a-z0-9_.-])", re.IGNORECASE)

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                if pattern.search(value):
                    new_value = pattern.sub(new_ref, value)
                    if new_value != value:
                        data[key] = new_value
                        modified = True
            elif isinstance(value, (dict, list)):
                if replace_references(value, old_ref, new_ref):
                    modified = True

    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, str):
                if pattern.search(item):
                    new_value = pattern.sub(new_ref, item)
                    if new_value != item:
                        data[i] = new_value
                        modified = True
            elif isinstance(item, (dict, list)):
                if replace_references(item, old_ref, new_ref):
                    modified = True

    return modified


def get_device_registry(
    ws: websocket.WebSocket, msg_id: int
) -> Tuple[Dict[str, Any], int]:
    """
    Fetches the device registry.
    Returns a dictionary of devices indexed by ID, and the updated msg_id.
    """
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "config/device_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    devices = {}
    if result["success"]:
        for d in result["result"]:
            devices[d["id"]] = d
    else:
        print("Failed to list devices.")

    return devices, msg_id


def find_related_automations(
    ws: websocket.WebSocket, entity_id: str, msg_id: int
) -> Tuple[List[str], int]:
    """
    Finds automations related to a given entity ID.
    Returns a list of automation entity IDs and the updated msg_id.
    """
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "search/related",
                "item_type": "entity",
                "item_id": entity_id,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    automations = []
    if result["success"]:
        if "automation" in result["result"]:
            automations = result["result"]["automation"]

    return automations, msg_id


def get_automation_config(
    ws: websocket.WebSocket, automation_entity_id: str, msg_id: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    """
    Fetches the configuration for a specific automation.
    Returns the config dict and the updated msg_id.
    """
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "automation/config",
                "entity_id": automation_entity_id,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        # The automation config is sometimes wrapped in a "config" key
        if "config" in result["result"]:
            return result["result"]["config"], msg_id
        return result["result"], msg_id
    return None, msg_id


def get_valid_services(ws: websocket.WebSocket, msg_id: int) -> Tuple[Set[str], int]:
    """
    Fetches all valid services.
    Returns a set of service IDs (domain.service) and the updated msg_id.
    """
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "get_services"}))
    result = ws.recv()
    result = json.loads(result)

    if not result["success"]:
        print("Failed to list services.")
        return set(), msg_id

    services = set()
    for domain, domain_services in result["result"].items():
        for service in domain_services:
            services.add(f"{domain}.{service}")
    return services, msg_id


def list_dashboards(
    ws: websocket.WebSocket, msg_id: int
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Lists all Lovelace dashboards.
    Returns a list of dashboard objects and the updated msg_id.
    """
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "lovelace/dashboards/list"}))
    result = ws.recv()
    result = json.loads(result)

    dashboards = []
    if result["success"]:
        dashboards = result["result"]

    return dashboards, msg_id


def get_dashboard_config(
    ws: websocket.WebSocket, url_path: Optional[str], msg_id: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    """
    Fetches the configuration for a specific dashboard.
    If url_path is None, fetches the default dashboard.
    Returns the config dict and the updated msg_id.
    """
    msg_id += 1
    payload = {"id": msg_id, "type": "lovelace/config"}
    if url_path:
        payload["url_path"] = url_path

    ws.send(json.dumps(payload))
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        return result["result"], msg_id
    return None, msg_id


def save_dashboard_config(
    ws: websocket.WebSocket,
    url_path: Optional[str],
    config_data: Dict[str, Any],
    msg_id: int,
) -> Tuple[bool, int]:
    """
    Saves the configuration for a specific dashboard.
    Returns True if successful, and the updated msg_id.
    """
    msg_id += 1
    payload = {"id": msg_id, "type": "lovelace/config/save", "config": config_data}
    if url_path:
        payload["url_path"] = url_path

    ws.send(json.dumps(payload))
    result = ws.recv()
    result = json.loads(result)

    return result["success"], msg_id
