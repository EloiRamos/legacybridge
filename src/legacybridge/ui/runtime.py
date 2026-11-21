"""Process-level wiring for the console: environment, one client and one service per process."""

from __future__ import annotations

import streamlit as st
from dotenv import load_dotenv

from ..bridge.client import LegacyClient, SimControlClient
from ..bridge.errors import BridgeError
from ..config import BridgeSettings
from ..service import BridgeService

_dotenv_loaded = False


def _load_env_once() -> None:
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv()
        _dotenv_loaded = True


def get_settings() -> BridgeSettings:
    _load_env_once()
    return BridgeSettings.from_env()


def build_service() -> BridgeService:
    settings = get_settings()
    client = LegacyClient(
        settings.legacy_base_url,
        settings.legacy_login,
        settings.legacy_secret,
        timeout=settings.legacy_timeout_sec,
        max_retries=settings.legacy_max_retries,
    )
    return BridgeService(client, SimControlClient(settings.legacy_base_url, timeout=settings.legacy_timeout_sec))


@st.cache_resource
def _service_resource() -> BridgeService:
    return build_service()


def get_service() -> BridgeService:
    return _service_resource()


def sim_control_responds(service: BridgeService) -> bool:
    """Whether the demo harness (/_sim/*) answers; used only to show the Reset demo data control.
    The answer is never used as evidence for any order outcome."""
    if service.sim is None:
        return False
    try:
        service.sim.list_faults()
        return True
    except BridgeError:
        return False
