"""Guard Playground: a local demo UI that runs AgentMailGuard on sample or custom emails.

A separate FastAPI + Jinja2 + htmx service (127.0.0.1:3010) that imports the guard library
(``mailguard``) in-process. It never writes to ``agentmailguard/`` or ``evaluation/results/``.
"""
