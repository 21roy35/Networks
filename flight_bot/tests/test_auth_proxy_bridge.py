from __future__ import annotations

import base64
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "scripts" / "auth_proxy_bridge.py"
SPEC = importlib.util.spec_from_file_location("auth_proxy_bridge", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _decoded_username() -> str:
    encoded = MODULE.ProxyHandler._authorization()
    return base64.b64decode(encoded).decode("utf-8").split(":", 1)[0]


def test_authorization_injects_city_before_sticky_session(tmp_path):
    session_file = tmp_path / "session"
    session_file.write_text("freshsession123", encoding="ascii")
    MODULE.ProxyHandler.upstream_username = (
        "geonode_user-type-residential-country-sa-"
        "session-old-lifetime-180"
    )
    MODULE.ProxyHandler.upstream_password = "secret"
    MODULE.ProxyHandler.target_city = "riyadh"
    MODULE.ProxyHandler.session_file = session_file

    assert _decoded_username() == (
        "geonode_user-type-residential-country-sa-city-riyadh-"
        "session-freshsession123-lifetime-180"
    )


def test_authorization_replaces_existing_city():
    MODULE.ProxyHandler.upstream_username = (
        "geonode_user-type-residential-country-sa-city-jeddah-"
        "session-current-lifetime-180"
    )
    MODULE.ProxyHandler.upstream_password = "secret"
    MODULE.ProxyHandler.target_city = "riyadh"
    MODULE.ProxyHandler.session_file = None

    assert "-country-sa-city-riyadh-session-current-" in _decoded_username()
