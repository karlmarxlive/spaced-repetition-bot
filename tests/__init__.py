"""Offline tests: ignore local secrets and reject network connections."""
import os
import socket

os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"

def deny_network(*args, **kwargs):
    raise AssertionError("Network access is forbidden in offline tests")

original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex

def guarded_connect(sock, address):
    # Windows asyncio uses a loopback socketpair for its internal wakeup pipe.
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
        return original_connect(sock, address)
    return deny_network()

def guarded_connect_ex(sock, address):
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
        return original_connect_ex(sock, address)
    return deny_network()

socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex
socket.create_connection = deny_network

import django
django.setup()
