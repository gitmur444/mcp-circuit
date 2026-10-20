#!/usr/bin/env python3
"""MCP over TCP, container side: the agent's stdio MCP config runs this; the real server runs on the host (condition
"host": true) — for MCPs that must launch or kill the app on the host itself.

  python3 bridge.py host.docker.internal 21001
"""
import socket, sys, threading

s = socket.create_connection((sys.argv[1], int(sys.argv[2])))


def up():
    for line in sys.stdin.buffer:
        s.sendall(line)
    s.shutdown(socket.SHUT_WR)


threading.Thread(target=up, daemon=True).start()
while True:
    b = s.recv(65536)
    if not b:
        break
    sys.stdout.buffer.write(b)
    sys.stdout.buffer.flush()
