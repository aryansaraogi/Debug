# broken_app

A deliberately broken Flask service used as the demo/test target for the DevOps MCP servers.

Planted bug: `app/routes.py` reads `user["email"]` but the repository returns rows keyed `mail`.
Every request to `/users/<id>` raises `KeyError: 'email'`, which surfaces as HTTP 500 in `logs/app.log`.
