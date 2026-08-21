import json
import ssl
import websocket
import config
import difflib
import requests
import re
from typing import List, Dict, Set, Tuple, Optional, Any, Union
from contextlib import contextmanager

# Determine the protocol based on TLS configuration
TLS_S = "s" if config.TLS else ""

# List of references to ignore (known false positives or intentionally missing)
IGNORED_REFERENCES = {
    "todo.add_item",  # Often flagged if no todo lists are active
}

# Regex patterns for entity ID matching
ENTITY_ID_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+$"
ENTITY_ID_IN_QUOTES_PATTERN = r'"([a-z0-9_]+\.[a-z0-9_]+)"'
COMMON_FALSE_POSITIVES = {
    "platform.state",
    "platform.numeric_state",
    "platform.template",
    "platform.time",
    "platform.sun",
    "platform.zone",
    "platform.webhook",
    "platform.mqtt",
}

# Known service domains
KNOWN_SERVICE_DOMAINS = {
    "homeassistant",
    "system_log",
    "logger",
    "persistent_notification",
    "notify",
    "tts",
    "frontend",
    "recorder",
    "history",
    "logbook",
}

# Common service verbs
COMMON_SERVICE_VERBS = {
    "turn_on",
    "turn_off",
    "toggle",
    "stop",
    "start",
    "restart",
    "reload",
    "create",
    "delete",
    "add_item",
    "remove_item",
    "snapshot",
    "play_media",
    "trigger",
}

# User prompt messages
PROMPT_APPLY_FIX = "  Apply a fix? (1-{max_suggestions}/N): "
PROMPT_APPLY_FIX_WITH_DELETE = "  Apply a fix? (1-{max_suggestions}/N/d=delete): "
PROMPT_CONFIRM_RENAME = "\nDo you want to proceed with renaming the entities? (y/N): "
PROMPT_DELETE_MEMBER = "  Delete this member? (y/N): "


def is_ignored(ref: str) -> bool:
    """
    Checks if a reference should be ignored.
    """
    if ref in IGNORED_REFERENCES:
        return True
    return False


def is_likely_service(ref: str) -> bool:
    """
    Determines if a reference is likely a service call rather than an entity.
    Checks against known service domains and common service verbs.
    """
    if "." not in ref:
        return False
    domain, name = ref.split(".", 1)

    # Check if domain is a known service domain
    if domain in KNOWN_SERVICE_DOMAINS:
        return True

    # Check if the service verb is common
    if name in COMMON_SERVICE_VERBS:
        return True

    return False


def prompt_apply_fix(num_suggestions: int) -> str:
    """
    Prompts user to apply a fix from a list of suggestions.
    Returns the user's input.
    """
    return input(PROMPT_APPLY_FIX.format(max_suggestions=num_suggestions))


def prompt_confirm_rename() -> str:
    """
    Prompts user to confirm entity renaming operation.
    Returns the user's input.
    """
    return input(PROMPT_CONFIRM_RENAME)


def prompt_delete_member() -> str:
    """
    Prompts user to confirm deletion of a group member.
    Returns the user's input.
    """
    return input(PROMPT_DELETE_MEMBER)


def prompt_apply_fix_with_delete(num_suggestions: int) -> str:
    """
    Prompts user to apply a fix from a list of suggestions, with delete option.
    Supports: 1-n (apply fix), N (skip), d (delete).
    Returns the user's input.
    """
    return input(PROMPT_APPLY_FIX_WITH_DELETE.format(max_suggestions=num_suggestions))


@contextmanager
def websocket_context():
    """
    Context manager for WebSocket connections.
    Ensures proper connection cleanup even if errors occur.

    Usage:
        with websocket_context() as ws:
            # use ws
    """
    ws = connect_websocket()
    try:
        yield ws
    finally:
        if ws:
            ws.close()


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


def get_entity_registry(
    ws: websocket.WebSocket, msg_id: int
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Fetches the entity registry as a list of full entries.

    Unlike get_valid_entities, which flattens the registry and the state machine
    into a set of entity IDs, this keeps every field -- notably device_id, which
    is what lets a caller join entities onto devices.

    Returns the list of entries and the updated msg_id.
    """
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "config/entity_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        return result["result"], msg_id

    print("Failed to list registry entities.")
    return [], msg_id


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


def find_related(
    ws: websocket.WebSocket, item_type: str, item_id: str, msg_id: int
) -> Tuple[Dict[str, List[str]], int]:
    """
    Finds everything Home Assistant considers related to an item.

    item_type is any type the search integration accepts -- "entity", "device",
    "area", "config_entry", "automation", "script", "scene", ... The result maps
    a related type to a list of its IDs, e.g.
    {"automation": ["automation.foo"], "area": ["office"]}. A related type with
    nothing in it is absent from the mapping rather than present and empty.

    Returns the mapping and the updated msg_id.
    """
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "search/related",
                "item_type": item_type,
                "item_id": item_id,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        return result["result"], msg_id
    return {}, msg_id


def find_related_automations(
    ws: websocket.WebSocket, entity_id: str, msg_id: int
) -> Tuple[List[str], int]:
    """
    Finds automations related to a given entity ID.
    Returns a list of automation entity IDs and the updated msg_id.
    """
    related, msg_id = find_related(ws, "entity", entity_id, msg_id)
    return related.get("automation", []), msg_id


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


def get_registry_entry(
    ws: websocket.WebSocket, entity_id: str, msg_id: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    """
    Fetches the entity registry entry for a specific entity.
    Returns the entry dict and the updated msg_id.
    """
    msg_id += 1
    ws.send(
        json.dumps(
            {"id": msg_id, "type": "config/entity_registry/get", "entity_id": entity_id}
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        return result["result"], msg_id
    return None, msg_id


def _rest_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {config.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _abort_options_flow(flow_id: str) -> None:
    """Abandons an in-progress options flow. Best effort; never raises."""
    try:
        requests.delete(
            f"http{TLS_S}://{config.HOST}/api/config/config_entries/options/flow/{flow_id}",
            headers=_rest_headers(),
            verify=config.SSL_VERIFY,
        )
    except Exception:
        pass


def update_config_entry_options(
    ws: websocket.WebSocket, entry_id: str, options: Dict[str, Any], msg_id: int
) -> Tuple[bool, int]:
    """
    Updates the options of a config entry by driving its options flow.

    `ws` and `msg_id` are vestigial -- the implementation is REST, and they are
    kept only so existing callers need no change. msg_id is returned unmodified.

    Home Assistant does NOT accept an `options` key on the WebSocket
    `config_entries/update` command; it answers `invalid_format: extra keys not
    allowed @ data['options']` (verified against 2026.8). Options belong to the
    integration's own options flow, which is REST-only:

        POST /api/config/config_entries/options/flow  {"handler": <entry_id>}
        POST /api/config/config_entries/options/flow/<flow_id>  {<field>: <value>}

    The flow REPLACES the options mapping rather than patching it, so `options`
    here is treated as an OVERLAY: every field the form declares is seeded from
    its own `description.suggested_value` (which is the entry's current value),
    then the caller's keys are written over the top. Passing only the one key
    you care about is therefore safe. Sending it bare is not -- a group helper
    given just {"entities": [...]} loses its `hide_members` and `name`.

    Returns True if the flow ran to completion, and the unmodified msg_id.
    """
    base = f"http{TLS_S}://{config.HOST}/api/config/config_entries/options/flow"
    headers = _rest_headers()

    try:
        response = requests.post(
            base,
            headers=headers,
            json={"handler": entry_id},
            verify=config.SSL_VERIFY,
        )
        if response.status_code != 200:
            print(
                f"Failed to start options flow for {entry_id}: "
                f"{response.status_code} {response.text}"
            )
            return False, msg_id
        flow = response.json()

        # A form may be followed by another form (multi-step flows). Keep
        # submitting until the flow stops asking. The bound is a safety net
        # against an integration that loops on a validation error we cannot see.
        for _ in range(10):
            flow_type = flow.get("type")

            if flow_type in ("create_entry", "abort"):
                if flow_type == "abort":
                    print(
                        f"Options flow for {entry_id} aborted: "
                        f"{flow.get('reason')}"
                    )
                    return False, msg_id
                return True, msg_id

            if flow_type != "form":
                print(f"Unexpected options flow step for {entry_id}: {flow_type}")
                _abort_options_flow(flow["flow_id"])
                return False, msg_id

            payload = {}
            for field in flow.get("data_schema") or []:
                name = field.get("name")
                if not name:
                    continue
                description = field.get("description") or {}
                if "suggested_value" in description:
                    payload[name] = description["suggested_value"]
                elif "default" in field:
                    payload[name] = field["default"]
            payload.update(options)

            response = requests.post(
                f"{base}/{flow['flow_id']}",
                headers=headers,
                json=payload,
                verify=config.SSL_VERIFY,
            )
            if response.status_code != 200:
                print(
                    f"Failed to submit options flow for {entry_id}: "
                    f"{response.status_code} {response.text}"
                )
                _abort_options_flow(flow["flow_id"])
                return False, msg_id
            flow = response.json()

            if flow.get("errors"):
                print(f"Options flow for {entry_id} rejected: {flow['errors']}")
                _abort_options_flow(flow["flow_id"])
                return False, msg_id

        print(f"Options flow for {entry_id} did not finish; giving up.")
        _abort_options_flow(flow["flow_id"])
        return False, msg_id
    except Exception as e:
        print(f"Exception while updating options for {entry_id}: {e}")
        return False, msg_id


def reload_config_entry(entry_id: str) -> bool:
    """
    Reloads a config entry so its entities pick up a changed title or options.

    There is no WebSocket command for this -- `config_entries/reload` answers
    `unknown_command` (verified against 2026.8). Only the REST endpoint exists.

    This matters after a rename: setting a config entry's title alone leaves the
    entity registry's `original_name` on the old value, so the entity ID that
    Home Assistant would generate does not change until the entry is reloaded.
    """
    url = (
        f"http{TLS_S}://{config.HOST}"
        f"/api/config/config_entries/entry/{entry_id}/reload"
    )
    try:
        response = requests.post(
            url, headers=_rest_headers(), verify=config.SSL_VERIFY
        )
        if response.status_code == 200:
            return True
        print(
            f"Failed to reload config entry {entry_id}: "
            f"{response.status_code} {response.text}"
        )
        return False
    except Exception as e:
        print(f"Exception while reloading config entry {entry_id}: {e}")
        return False


def get_scene_config(
    ws: websocket.WebSocket, scene_entity_id: str, msg_id: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "scene/config",
                "entity_id": scene_entity_id,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        if "config" in result["result"]:
            return result["result"]["config"], msg_id
        return result["result"], msg_id
    return None, msg_id


def save_scene_config(scene_config: Dict[str, Any]) -> bool:
    scene_id = scene_config.get("id")
    if not scene_id:
        print("Error: Scene config missing ID.")
        return False

    url = f"http{TLS_S}://{config.HOST}/api/config/scene/config/{scene_id}"
    headers = {
        "Authorization": f"Bearer {config.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url, headers=headers, json=scene_config, verify=config.SSL_VERIFY
        )
        if response.status_code == 200:
            return True
        else:
            print(f"Failed to save scene {scene_id}: {response.text}")
            return False
    except Exception as e:
        print(f"Exception while saving scene {scene_id}: {e}")
        return False


def get_script_config(
    ws: websocket.WebSocket, script_entity_id: str, msg_id: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    """
    Fetches the configuration for a specific script.
    Returns the config dict and the updated msg_id.
    """
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "script/config",
                "entity_id": script_entity_id,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        if "config" in result["result"]:
            return result["result"]["config"], msg_id
        return result["result"], msg_id
    return None, msg_id


def save_script_config(script_config: Dict[str, Any]) -> bool:
    """
    Saves a script configuration to Home Assistant via the HTTP API.
    """
    # Scripts in UI have a unique_id which is used as the ID in the URL
    # However, the config object itself might not have 'id' field like automations do.
    # It usually has 'unique_id'.

    script_id = script_config.get("unique_id")
    if not script_id:
        # Fallback: sometimes 'id' is used?
        script_id = script_config.get("id")

    if not script_id:
        print("Error: Script config missing unique_id.")
        return False

    url = f"http{TLS_S}://{config.HOST}/api/config/script/config/{script_id}"
    headers = {
        "Authorization": f"Bearer {config.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url, headers=headers, json=script_config, verify=config.SSL_VERIFY
        )
        if response.status_code == 200:
            return True
        else:
            print(f"Failed to save script {script_id}: {response.text}")
            return False
    except Exception as e:
        print(f"Exception while saving script {script_id}: {e}")
        return False
