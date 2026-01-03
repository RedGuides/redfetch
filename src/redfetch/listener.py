import asyncio
import traceback
import webbrowser
from typing import Any, Dict, Optional

import aiosqlite
from aiohttp import web

from redfetch import store
from redfetch import sync
from redfetch.special import compute_special_status


REDGUIDES_ORIGIN = "https://www.redguides.com"


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Simple CORS middleware allowing RedGuides origin."""
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)

    resp.headers["Access-Control-Allow-Origin"] = REDGUIDES_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp


async def _get_root_version_local_async(db_name: str, resource_id: str) -> Optional[int]:
    """Async equivalent of store.get_root_version_local for roots (parent_id=0)."""
    db_path = store.get_db_path(db_name)
    try:
        async with aiosqlite.connect(db_path, timeout=30.0) as conn:
            async with conn.execute(
                "SELECT version_local FROM downloads WHERE parent_id = 0 AND resource_id = ?",
                (int(resource_id),),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else None
    except aiosqlite.OperationalError as e:
        if "no such table" in str(e):
            # Table doesn't exist yet; treat as not installed
            return None
        print("Database error during health check:", str(e))
        traceback.print_exc()
        raise


async def handle_health(request: web.Request) -> web.Response:
    db_name = request.app["db_name"]

    resource_id = request.query.get("resource_id")
    remote_version_str = request.query.get("remote_version")

    if resource_id and remote_version_str is not None:
        try:
            remote_version = int(remote_version_str)
        except ValueError:
            return web.json_response({"success": False, "message": "Invalid remote_version"}, status=400)

        try:
            version_local = await _get_root_version_local_async(db_name, resource_id)
        except Exception:
            return web.json_response({"success": False, "message": "Database error"}, status=500)

        if version_local is None:
            return web.json_response({"action": "install"})
        elif version_local < remote_version:
            return web.json_response({"action": "update"})
        else:
            return web.json_response({"action": "re-install"})

    return web.json_response({"status": "up"})


async def handle_download(request: web.Request) -> web.Response:
    app = request.app
    db_name = app["db_name"]
    headers = app["headers"]

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"success": False, "message": "JSON body is required."}, status=400)

    resource_id = payload.get("resource_id")
    if resource_id is None:
        return web.json_response({"success": False, "message": "Resource ID is required."}, status=400)

    try:
        resource_id_str = str(resource_id)
        db_path = store.get_db_path(db_name)
        success = await sync.run_sync(db_path, headers, resource_ids=[resource_id_str])
        if success:
            return web.json_response({"success": True, "message": "Download completed successfully."})
        return web.json_response({"success": False, "message": "Download failed due to internal error."}, status=500)
    except Exception as e:
        print("Error during download:", str(e))
        traceback.print_exc()
        return web.json_response({"success": False, "message": f"Download failed: {e}"}, status=500)


async def handle_download_watched(request: web.Request) -> web.Response:
    app = request.app
    db_name = app["db_name"]
    headers = app["headers"]

    try:
        db_path = store.get_db_path(db_name)
        success = await sync.run_sync(db_path, headers)
        if success:
            return web.json_response(
                {"success": True, "message": "All watched resources downloaded successfully."}
            )
        return web.json_response(
            {"success": False, "message": "Download of one or more resources failed."}, status=500
        )
    except Exception as e:
        print("Error during download of watched resources:", str(e))
        traceback.print_exc()
        return web.json_response({"success": False, "message": f"Download failed: {e}"}, status=500)


async def handle_reset_download_date(request: web.Request) -> web.Response:
    app = request.app
    db_name = app["db_name"]

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"success": False, "message": "JSON body is required."}, status=400)

    resource_id = payload.get("resource_id")
    if not resource_id:
        return web.json_response({"success": False, "message": "Resource ID is required."}, status=400)

    try:
        int(str(resource_id))
    except ValueError:
        return web.json_response({"success": False, "message": "Invalid resource ID format."}, status=400)

    def _reset() -> bool:
        try:
            with store.get_db_connection(db_name) as conn:
                cursor = conn.cursor()
                store.reset_download_date_for_resource(cursor, str(resource_id))
                conn.commit()
            return True
        except Exception as e:
            print("Error during resetting download date:", str(e))
            traceback.print_exc()
            return False

    ok = await asyncio.to_thread(_reset)
    if ok:
        return web.json_response({"success": True, "message": "Download date reset successfully."})
    return web.json_response({"success": False, "message": "Reset failed."}, status=500)


async def handle_category_map(request: web.Request) -> web.Response:
    category_map = request.app["category_map"]
    return web.json_response(list(category_map.keys()))


async def handle_special_resource_ids(request: web.Request) -> web.Response:
    status = compute_special_status(None)
    special_resource_ids = [int(rid) for rid, info in status.items() if info["is_special"]]
    print(f"special_resource_ids: {special_resource_ids}")
    return web.json_response(special_resource_ids)


async def create_app(
    settings,
    db_name: str,
    headers: dict,
    special_resources,
    category_map,
) -> web.Application:
    """Create the aiohttp application for the RedGuides interface."""
    app = web.Application(middlewares=[cors_middleware])

    # Store shared context
    app["settings"] = settings
    app["db_name"] = db_name
    app["headers"] = headers
    app["special_resources"] = special_resources
    app["category_map"] = category_map

    app.router.add_get("/health", handle_health)
    app.router.add_post("/download", handle_download)
    app.router.add_post("/download-watched", handle_download_watched)
    app.router.add_post("/reset-download-date", handle_reset_download_date)
    app.router.add_get("/category-map", handle_category_map)
    app.router.add_get("/special-resource-ids", handle_special_resource_ids)

    return app


async def run_server_async(
    settings,
    db_name: str,
    headers: dict,
    special_resources,
    category_map,
) -> None:
    """Run the interface server until cancelled."""
    app = await create_app(settings, db_name, headers, special_resources, category_map)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 7734)
    await site.start()

    webbrowser.open_new("https://www.redguides.com/cookie/set_marker.php")
    print("Server starting. Browse resources on https://www.redguides.com/community/resources")

    try:
        # Wait indefinitely until the task is cancelled by the caller (CLI or TUI).
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        print("Server task cancelled, shutting down...")
    finally:
        await runner.cleanup()
        print("Server stopped.")

