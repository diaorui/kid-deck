from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from fastapi import APIRouter


class Plugin(ABC):
    name: str = ""
    title: str = ""
    icon: str = ""
    order: int = 99

    def __init__(self, controller: Any, config: dict):
        self.controller = controller
        self.config = config
        self.router = APIRouter(prefix=f"/api/{self.name}")
        self.running = False

    @abstractmethod
    def ui_section(self) -> str:
        ...

    def ui_js(self) -> str:
        return ""

    def register_routes(self, app):
        app.include_router(self.router)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False
