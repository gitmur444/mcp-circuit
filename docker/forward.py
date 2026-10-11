#!/usr/bin/env python3
"""TCP forwarder: 127.0.0.1:<listen> -> 127.0.0.1:<target>. Lets the container reach the app,
which listens on localhost only.  python3 forward.py [listen=19998] [target=9998] [bind=127.0.0.1]"""
import socket, sys, threading

LISTEN = int(sys.argv[1]) if len(sys.argv) > 1 else 19998
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 9998
BIND = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"  # Docker Desktop routes host.docker.internal to the host loopback


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve(c):
    try:
        u = socket.create_connection(("127.0.0.1", TARGET), 5)
    except OSError:
        c.close(); return
    u.settimeout(None)  # otherwise the connect timeout (5 s) stays on recv and kills silent verification
    for s in (c, u):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((BIND, LISTEN))
srv.listen(64)
print(f"forward {BIND}:{LISTEN} -> 127.0.0.1:{TARGET}", flush=True)
while True:
    c, _ = srv.accept()
    serve(c)
