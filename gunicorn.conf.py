"""Gunicorn settings. Env: PORT (default 8080), WORKERS (2), THREADS (2, at least 2)."""

import os
import threading

wsgi_app = "app:app"
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = int(os.environ.get("WORKERS", "2"))
worker_class = "gthread"
threads = max(2, int(os.environ.get("THREADS", "2")))  # a free thread always answers /healthcheck
preload_app = True  # import PolicyEngine and warm up once, in the master, before the fork
keepalive = 0  # a retried busy 503 must not ride a kept-alive socket back to the same busy process
# gthread's --timeout does not bound a request; app.watchdog does.


def post_fork(server, worker):
    import app

    threading.Thread(target=app.watchdog, daemon=True).start()
