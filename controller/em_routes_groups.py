"""
Speaker group routes: read the arrangement (any signed-in user) and replace it
(admin). The body of a save is the whole list, so the dashboard edits a copy
and sends it back, as it does for schedule sharing.
"""

from __future__ import annotations

from aiohttp import web

import em_auth as auth
import em_groups
import em_http


def register(app: web.Application) -> None:
    app.router.add_get("/api/speaker-groups", _get_groups)
    app.router.add_put("/api/speaker-groups", _put_groups)


@auth.require_auth
async def _get_groups(request: web.Request) -> web.Response:
    return em_http.ok(await em_groups.view())


@auth.require_admin
async def _put_groups(request: web.Request) -> web.Response:
    body = await em_http.json_body(request)
    try:
        await em_groups.validate_members(body.get("groups"))
        await em_groups.set_groups(body.get("groups"))
    except ValueError as e:
        return em_http.error("invalid_groups", str(e), 400)
    await em_groups.reconcile()
    return em_http.ok(await em_groups.view())
