"""Serveur de démonstration des secrets volatils (`servers/mcp_secret_demo.py`)."""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_secret_demo as demo
from mcp_base import ConfigIncomplete, CredentialsRejected

MARKER = {"volatile": "Mot de passe de démonstration"}


def test_check_config_accepts_the_marker_in_place():
    demo.check_config({"login": "demo", "password": MARKER})
    demo.check_config({"login": "demo", "instances": [{"password": MARKER}] * 3})


@pytest.mark.parametrize("config", [
    {"password": MARKER},
    {"login": " ", "password": MARKER},
    {"login": "demo"},
    {"login": "demo", "instances": [{"password": MARKER}, {}]},
])
def test_check_config_refuses_an_incomplete_config(config):
    with pytest.raises(ConfigIncomplete):
        demo.check_config(config)


async def test_expected_value_builds_and_whoami_never_gives_the_password():
    server = demo.build({"login": "demo", "password": "miaou"})
    assert server.instructions == demo.INSTRUCTIONS
    result = await server.call_tool("whoami", {})
    text = result.content[0].text
    assert text == "Authentifié en tant que « demo » (1 instance)."
    assert "miaou" not in text


def test_wrong_value_is_rejected_without_quoting_it():
    with pytest.raises(CredentialsRejected) as info:
        demo.build({"login": "demo", "password": "pas-la-bonne"})
    assert "pas-la-bonne" not in str(info.value)


def test_one_bad_instance_rejects_them_all():
    with pytest.raises(CredentialsRejected):
        demo.build({"login": "demo", "instances": [{"password": "miaou"}, {"password": "x"}, {"password": "miaou"}]})


def test_reserved_value_simulates_an_unreachable_directory():
    with pytest.raises(ConnectionError):
        demo.build({"login": "demo", "password": demo.UNREACHABLE})


def test_expected_value_comes_from_the_config_then_the_environment(monkeypatch):
    demo.build({"login": "demo", "password": "autre", "expected": "autre"})
    monkeypatch.setenv("MIAOU_SECRET_DEMO_EXPECTED", "env")
    demo.build({"login": "demo", "password": "env"})


@pytest.mark.parametrize("raw, expected", [(None, 0.0), ("x", 0.0), (-3, 0.0), (99, 10.0), (float("nan"), 0.0), (0.5, 0.5)])
def test_delay_is_clamped(raw, expected):
    assert demo._delay({"delay_s": raw} if raw is not None else {}) == expected


def test_help_answers_without_any_password(tmp_path):
    """D2-1 : `--help` se traite avant toute authentification."""
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("MIAOU_SECRET_DEMO")}
    for password in (None, "mauvais"):
        e = dict(env)
        if password:
            e["MIAOU_SECRET_DEMO_PASSWORD"] = password
        r = subprocess.run(
            [sys.executable, str(_SERVERS / "mcp_secret_demo.py"), "--help"],
            capture_output=True, text=True, env=e, timeout=60, cwd=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert "--transport" in r.stdout
