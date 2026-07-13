import importlib.util
import inspect
import os
import sys
from collections import defaultdict
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from camera_client import CameraClient, check_deps
from plugins import Plugin


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"Config not found: {p}")
        example = p.with_suffix(".yaml.example")
        if example.exists():
            print(f"Copy {example.name} to {p.name} and edit with your settings:")
            print(f"  cp {example.name} {p.name}")
        sys.exit(1)
    with open(p) as f:
        cfg = yaml.safe_load(f)
    if cfg is None:
        print(f"Empty config: {p}")
        sys.exit(1)
    return cfg


def discover_plugins(controller, plugins_config: dict) -> list[Plugin]:
    plugin_dir = Path(__file__).parent / "plugins"
    found = []

    for pyfile in sorted(plugin_dir.glob("*.py")):
        if pyfile.name == "__init__.py":
            continue

        module_name = f"plugins.{pyfile.stem}"
        spec = importlib.util.spec_from_file_location(module_name, pyfile)
        if spec is None or spec.loader is None:
            continue

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Plugin) and obj is not Plugin:
                plugin_config = plugins_config.get(obj.name, {})
                plugin = obj(controller, plugin_config)
                found.append(plugin)
                print(f"Loaded plugin: {obj.name} ({obj.title})")
                break

    found.sort(key=lambda p: p.order)
    return found


class Controller:
    def __init__(self):
        self.camera: CameraClient | None = None
        self.plugins: list[Plugin] = []
        self._listeners: dict[str, list] = defaultdict(list)
        self._plugin_map: dict[str, Plugin] = {}

    def on(self, event: str, callback):
        self._listeners[event].append(callback)

    def emit(self, event: str, **data):
        for cb in self._listeners[event]:
            cb()

    def get_plugin(self, name: str) -> Plugin | None:
        return self._plugin_map.get(name)


def main():
    check_deps()
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)

    controller = Controller()
    controller.config = cfg

    camera_cfg = cfg.get("camera", {})
    controller.camera = CameraClient(camera_cfg)
    controller.camera.connect()

    plugins_cfg = cfg.get("plugins", {})
    controller.plugins = discover_plugins(controller, plugins_cfg)
    controller._plugin_map = {p.name: p for p in controller.plugins}

    app = FastAPI(title="KidDeck")
    templates = Jinja2Templates(directory=Path(__file__).parent)

    ap = controller.get_plugin("audio_player")
    if ap:
        ap.scan_files()
        ap.build_cache()

    for plugin in controller.plugins:
        plugin.register_routes(app)
        plugin.start()

    ap = controller.get_plugin("audio_player")
    alarm_plugin = controller.get_plugin("alarm")
    if ap and alarm_plugin:
        controller.on("alarm:triggered", lambda: ap.pause())
        controller.on("alarm:finished", lambda: ap.resume())
        print("Wired: alarm → audio player pause/resume")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request, "controller.html",
            {"plugins": controller.plugins},
        )

    @app.get("/api/status")
    async def status():
        camera_ok = controller.camera.is_connected()
        return {
            "camera_connected": camera_ok,
            "plugins": [p.name for p in controller.plugins],
        }

    @app.post("/api/restart")
    async def restart():
        import asyncio
        asyncio.create_task(_do_restart(controller))
        return {"ok": True}

    server_cfg = cfg.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8080)

    print(f"Controller starting on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


async def _do_restart(controller: Controller):
    import asyncio
    await asyncio.sleep(0.3)
    print("Restarting controller...", flush=True)
    for plugin in controller.plugins:
        plugin.stop()
    controller.camera.close()
    os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])


if __name__ == "__main__":
    main()
