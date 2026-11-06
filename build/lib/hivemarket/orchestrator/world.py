"""Run identities. (Game generation lives in hivemarket/game/graph.py.)"""

import secrets


def new_token() -> str:
    return secrets.token_hex(16)
