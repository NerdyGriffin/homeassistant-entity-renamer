import json
import ssl
import websocket
import config
import tabulate
from collections import defaultdict

tabulate.PRESERVE_WHITESPACE = True

# Determine the protocol based on TLS configuration
TLS_S = 's' if config.TLS else ''

def list_platforms():
    websocket_url = f'ws{TLS_S}://{config.HOST}/api/websocket'
    sslopt = {"cert_reqs": ssl.CERT_NONE} if not config.SSL_VERIFY else {}
    ws = websocket.WebSocket(sslopt=sslopt)
    ws.connect(websocket_url)

    auth_req = ws.recv()

    # Authenticate with Home Assistant
    auth_msg = json.dumps({"type": "auth", "access_token": config.ACCESS_TOKEN})
    ws.send(auth_msg)
    auth_result = ws.recv()
    auth_result = json.loads(auth_result)
    if auth_result["type"] != "auth_ok":
        print("Authentication failed. Check your access token.")
        return

    # List registry entries
    msg_id = 1
    ws.send(json.dumps({"id": msg_id, "type": "config/entity_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        entities = result["result"]
        
        platform_counts = defaultdict(int)
        platform_examples = defaultdict(list)

        for e in entities:
            platform = e.get("platform")
            platform_counts[platform] += 1
            if len(platform_examples[platform]) < 3:
                platform_examples[platform].append(e['entity_id'])

        # Prepare table
        table_data = []
        for platform, count in sorted(platform_counts.items(), key=lambda item: item[1], reverse=True):
            examples = ", ".join(platform_examples[platform])
            table_data.append((platform, count, examples))

        print(tabulate.tabulate(table_data, headers=["Platform", "Count", "Examples"], tablefmt="github"))

    else:
        print("Failed to list entities.")

    ws.close()

if __name__ == "__main__":
    list_platforms()
