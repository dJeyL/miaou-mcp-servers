"""Config en couches (`mcp_proxy/layers.py`) et dossier `state/`."""
import json
import stat
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp_proxy import entry
from mcp_proxy.layers import (
    interpolate_env,
    load_layers,
    mask_secrets,
    merge_patch,
    migrate_local,
    needs_slimming,
    resolve_chain,
    slim,
    strip_comments,
)
from mcp_proxy.state import (
    default_tokens_path,
    default_tools_cache_path,
    migrate_legacy_state,
    tools_cache_beside,
)


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# merge_patch : les vecteurs de l'annexe A de la RFC 7386
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "target, patch, expected",
    [
        ({"a": "b"}, {"a": "c"}, {"a": "c"}),
        ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
        ({"a": "b"}, {"a": None}, {}),
        ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
        ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
        ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
        ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
        ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
        (["a", "b"], ["c", "d"], ["c", "d"]),
        ({"a": "b"}, ["c"], ["c"]),
        ({"a": "foo"}, None, None),
        ({"a": "foo"}, "bar", "bar"),
        ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
        ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
        ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
    ],
)
def test_merge_patch_rfc7386_vectors(target, patch, expected):
    assert merge_patch(target, patch) == expected


def test_merge_patch_does_not_mutate_its_inputs():
    target = {"a": {"b": 1}}
    patch = {"a": {"c": 2}}
    merge_patch(target, patch)
    assert target == {"a": {"b": 1}}
    assert patch == {"a": {"c": 2}}


# ---------------------------------------------------------------------------
# Chaîne
# ---------------------------------------------------------------------------

def test_default_chain_reads_present_layers_in_order(tmp_path):
    _write(tmp_path / "config.defaults.json", {"port": 1, "host": "a", "mcpServers": {
        "web": {"type": "inprocess", "module": "mcp_web", "config": {"fetch": True}}}})
    _write(tmp_path / "config.site.json", {"host": "b"})
    _write(tmp_path / "config.json", {"port": 2, "mcpServers": {
        "web": {"config": {"fetch": False}}}})
    paths, local = resolve_chain(None, tmp_path)
    assert [p.name for p in paths] == ["config.defaults.json", "config.site.json", "config.json"]
    cfg = load_layers(paths, local).cfg
    assert cfg["port"] == 2 and cfg["host"] == "b"
    assert cfg["mcpServers"]["web"] == {
        "type": "inprocess", "module": "mcp_web", "config": {"fetch": False}}


def test_default_chain_skips_absent_layers_but_keeps_config_json_as_local(tmp_path):
    """Sans config.json, le maillon local reste config.json : c'est là que
    --migrate-config écrirait et que state/ se range."""
    _write(tmp_path / "config.defaults.json", {"port": 1})
    paths, local = resolve_chain(None, tmp_path)
    assert [p.name for p in paths] == ["config.defaults.json"]
    assert local == tmp_path / "config.json"


def test_default_chain_without_any_layer_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="aucune config"):
        resolve_chain(None, tmp_path)


def test_explicit_chain_replaces_the_default_and_requires_every_file(tmp_path):
    """`--config autre.json` seul lit ce seul fichier, comme avant les couches :
    une config complète d'un autre nom ne se voit rien ajouter."""
    _write(tmp_path / "config.defaults.json", {"port": 1, "host": "défaut"})
    other = _write(tmp_path / "autre.json", {"port": 9})
    paths, local = resolve_chain([str(other)])
    assert paths == [other] and local == other
    assert "host" not in load_layers(paths, local).cfg
    with pytest.raises(ValueError, match="introuvable"):
        resolve_chain([str(other), str(tmp_path / "absent.json")])


def test_port_is_required_on_the_merged_result_not_per_layer(tmp_path):
    base = _write(tmp_path / "a.json", {"port": 1})
    top = _write(tmp_path / "b.json", {"host": "x"})
    assert load_layers([base, top]).cfg["port"] == 1
    with pytest.raises(ValueError, match="port"):
        load_layers([top])


def test_a_layer_must_be_a_json_object(tmp_path):
    with pytest.raises(ValueError, match="objet JSON"):
        load_layers([_write(tmp_path / "a.json", [1])])


def test_miaou_dist_resolves_against_the_layer_that_declares_it(tmp_path):
    """Un `miaou_dist` relatif se lit contre SON fichier : une couche placée
    ailleurs qui ne le redéclare pas ne déplace pas le chemin."""
    site = tmp_path / "site"
    site.mkdir()
    base = _write(site / "base.json", {"port": 1, "miaou_dist": "dist"})
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    top = _write(elsewhere / "top.json", {"host": "x"})
    assert load_layers([base, top]).miaou_dist_base == base
    top2 = _write(elsewhere / "top2.json", {"miaou_dist": "autre"})
    assert load_layers([base, top2]).miaou_dist_base == top2


# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------

def test_interpolation_forms():
    env = {"SET": "v", "EMPTY": ""}
    unset: list = []
    out = interpolate_env({
        "a": "${SET}", "b": "x-${SET}-y", "c": "${EMPTY:-d}", "d": "${EMPTY-d}",
        "e": "${NOPE:-d}", "f": "$$SET", "g": "pa$word", "h": ["${SET}"], "i": 3,
    }, env, unset)
    assert out == {
        "a": "v", "b": "x-v-y", "c": "d", "d": "", "e": "d", "f": "$SET",
        "g": "pa$word", "h": ["v"], "i": 3,
    }
    assert unset == []


def test_unset_variable_becomes_empty_and_is_reported_with_its_key():
    unset: list = []
    out = interpolate_env({"mcpServers": {"web": {"config": {"k": "${MISSING}"}}}}, {}, unset)
    assert out["mcpServers"]["web"]["config"]["k"] == ""
    assert unset == [("MISSING", "mcpServers.web.config.k")]


def test_underscore_keys_are_left_verbatim():
    """`_comment` cite la syntaxe sans l'employer."""
    unset: list = []
    data = {"_comment": "écrire ${VAR}", "_example": {"k": "${VAR}"}}
    assert interpolate_env(data, {}, unset) == data
    assert unset == []


def test_disabled_block_is_interpolated_but_its_missing_vars_stay_quiet():
    """`--auth` réveille un bloc `auth` neutralisé : il doit être prêt."""
    unset: list = []
    out = interpolate_env(
        {"auth": {"disabled": True, "issuer_url": "${ISS}", "x": "${GONE}"}},
        {"ISS": "http://as"}, unset,
    )
    assert out["auth"]["issuer_url"] == "http://as"
    assert unset == []


def test_interpolation_happens_after_merge(tmp_path, monkeypatch):
    """Une valeur littérale de config.json qui remplace un `${VAR}` de la
    couche du dessous n'exige rien de l'environnement."""
    monkeypatch.delenv("SITE_SECRET", raising=False)
    base = _write(tmp_path / "a.json", {"port": 1, "secret": "${SITE_SECRET}"})
    top = _write(tmp_path / "b.json", {"secret": "littéral"})
    loaded = load_layers([base, top])
    assert loaded.cfg["secret"] == "littéral"
    assert loaded.unset_vars == []


def test_mask_secrets():
    cfg = {"api_key": "k", "client_secret": "s", "password": "", "headers": {
        "Authorization": "Bearer x", "X-API-Key": "k", "X-Auth": "a", "Accept": "j"},
        "url": "u", "nested": [{"token": "t"}], "LOGS_PASS": "p", "DB_PWD": "q",
        "passthrough": "visible", "username": "u"}
    assert mask_secrets(cfg) == {"api_key": "***", "client_secret": "***", "password": "",
                                 "headers": {"Authorization": "***", "X-API-Key": "***",
                                             "X-Auth": "***", "Accept": "j"},
                                 "url": "u", "nested": [{"token": "***"}],
                                 "LOGS_PASS": "***", "DB_PWD": "***",
                                 "passthrough": "visible", "username": "u"}


def test_mask_secrets_keeps_public_oauth_settings_readable():
    """Ce qu'on vient lire dans un diagnostic OAuth ne doit pas sortir masqué."""
    cfg = {"token_endpoint": "https://as/token", "authorization_endpoint": "https://as/auth",
           "token_endpoint_auth_method": "client_secret_post", "redirect_uri": "http://cb",
           "issuer_url": "https://as"}
    assert mask_secrets(cfg) == cfg


def test_mask_secrets_masks_every_env_value_and_url_passwords():
    cfg = {"env": {"HOME_DIR": "/x", "DB": "mysql://me:pw@h/db"},
           "url": "https://me:pw@h/mcp", "other": "https://h/mcp?a=b@c"}
    assert mask_secrets(cfg) == {"env": {"HOME_DIR": "***", "DB": "***"},
                                 "url": "https://me:***@h/mcp", "other": "https://h/mcp?a=b@c"}


# ---------------------------------------------------------------------------
# Allègement
# ---------------------------------------------------------------------------

def _effective(base, local):
    return strip_comments(merge_patch(base, local))


def test_slim_keeps_only_what_differs_and_drops_every_comment():
    base = {"port": 1, "_comment": "x", "mcpServers": {
        "web": {"type": "inprocess", "config": {"order": ["a", "b"]}, "_comment": "web"},
        "docs": {"disabled": True, "_comment": "docs"}}}
    local = {"port": 1, "_comment": "ancien texte", "mcpServers": {
        "web": {"type": "inprocess", "config": {"order": ["b"]}, "_comment": "vieux"},
        "docs": {"disabled": True, "_comment": "autre"},
        "mine": {"command": "x", "_comment": "à moi"}}}
    result = slim(base, local)
    assert result.content == {"mcpServers": {
        "web": {"config": {"order": ["b"]}}, "mine": {"command": "x"}}}
    assert _effective(base, result.content) == _effective(base, local)


def test_slim_lists_inherited_keys_without_writing_them():
    """Un serveur absent de la copie locale revient actif par la fusion,
    allègement ou non : il est signalé, pas neutralisé d'office — dans un
    config.json déjà léger, l'absence veut dire « hériter »."""
    base = {"port": 1, "mcpServers": {"bench": {"module": "b"}, "_example": {}}}
    local = {"port": 1, "mcpServers": {}}
    result = slim(base, local)
    assert result.content == {}
    assert result.inherited == ["mcpServers.bench"]


def test_slim_on_a_full_copy_of_the_real_defaults_is_empty():
    defaults = json.loads((_ROOT / "config.defaults.json").read_text())
    result = slim(defaults, json.loads(json.dumps(defaults)))
    assert result.content == {}
    assert result.inherited == []


def test_slim_preserves_the_effective_config_of_a_legacy_copy():
    """Copie ancienne du sample, retouchée : clef, ordre, serveur ajouté,
    commentaires périmés, une clé du sample absente (ajoutée après la copie)."""
    defaults = json.loads((_ROOT / "config.defaults.json").read_text())
    legacy = json.loads(json.dumps(defaults))
    legacy["mcpServers"]["web"]["config"]["search"]["brave"]["api_key"] = "ma-clef"
    legacy["mcpServers"]["web"]["config"]["search"]["order"] = ["ddg"]
    legacy["mcpServers"]["web"]["_comment"] = "texte d'une version précédente"
    legacy["mcpServers"]["perso"] = {"type": "http", "url": "http://x/mcp"}
    del legacy["mcpServers"]["web"]["config"]["fetch"]
    result = slim(defaults, legacy)
    assert result.content == {"mcpServers": {
        "web": {"config": {"search": {"order": ["ddg"], "brave": {"api_key": "ma-clef"}}}},
        "perso": {"type": "http", "url": "http://x/mcp"},
    }}
    assert result.inherited == ["mcpServers.web.config.fetch"]
    assert _effective(defaults, result.content) == _effective(defaults, legacy)


def test_migrate_local_writes_slim_file_and_backup_with_the_same_mode(tmp_path):
    base = _write(tmp_path / "config.defaults.json", {"port": 1, "host": "h"})
    local = _write(tmp_path / "config.json", {"port": 1, "host": "h", "k": "secret"})
    local.chmod(0o600)
    original = local.read_text()
    assert needs_slimming([base, local], local)

    migrate_local([base, local], local)

    assert json.loads(local.read_text()) == {"k": "secret"}
    backup = tmp_path / "config.json.bak"
    assert backup.read_text() == original
    assert stat.S_IMODE(local.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert not needs_slimming([base, local], local)


def test_migrate_local_refuses_to_overwrite_a_backup(tmp_path):
    base = _write(tmp_path / "config.defaults.json", {"port": 1})
    local = _write(tmp_path / "config.json", {"port": 1})
    (tmp_path / "config.json.bak").write_text("original d'avant")
    with pytest.raises(ValueError, match="existe déjà"):
        migrate_local([base, local], local)
    assert (tmp_path / "config.json.bak").read_text() == "original d'avant"


def test_migrate_local_refuses_a_single_layer(tmp_path):
    local = _write(tmp_path / "config.json", {"port": 1})
    with pytest.raises(ValueError, match="seule couche"):
        migrate_local([local], local)
    assert not needs_slimming([local], local)


def test_migrate_local_only_subtracts_the_layers_before_it(tmp_path):
    """Une couche placée APRÈS le maillon local n'est pas une base."""
    a = _write(tmp_path / "a.json", {"port": 1})
    local = _write(tmp_path / "b.json", {"port": 1, "x": 1})
    c = _write(tmp_path / "c.json", {"x": 1})
    migrate_local([a, local, c], local)
    assert json.loads(local.read_text()) == {"x": 1}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_print_config_masks_and_exits_without_serving(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LAYER_TEST_KEY", "vraie-clef")
    _write(tmp_path / "config.defaults.json", {"port": 1, "mcpServers": {}})
    _write(tmp_path / "config.json", {"api_key": "${LAYER_TEST_KEY}", "host": "${LAYER_TEST_KEY}"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mcp_proxy", "--print-config"])
    entry.main()
    out = json.loads(capsys.readouterr().out)
    assert out == {"port": 1, "mcpServers": {}, "api_key": "***", "host": "vraie-clef"}


def test_migrate_config_flag_rewrites_and_exits(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "config.defaults.json", {"port": 1, "mcpServers": {"bench": {"module": "b"}}})
    _write(tmp_path / "config.json", {"port": 1, "_comment": "c", "mcpServers": {}})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mcp_proxy", "--migrate-config"])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
    assert json.loads((tmp_path / "config.json").read_text()) == {}
    assert "mcpServers.bench" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# state/
# ---------------------------------------------------------------------------

def test_state_paths(tmp_path):
    assert default_tokens_path(tmp_path / "config.json") == tmp_path / "state" / "tokens.json"
    assert default_tools_cache_path(tmp_path / "config.json") == tmp_path / "state" / "tools-cache.json"
    # Un autre nom de config : son propre sous-dossier, deux instances lancées
    # du même dossier ne partagent pas leurs jetons.
    assert default_tokens_path(tmp_path / "autre.json") == tmp_path / "state" / "autre" / "tokens.json"
    # --tokens-file explicite : le cache reste où il était.
    assert tools_cache_beside(tmp_path / "x-tokens.json") == tmp_path / "x-tools.json"


def test_legacy_state_is_moved_not_rewritten(tmp_path):
    local = tmp_path / "config.json"
    tokens = tmp_path / "config-tokens.json"
    tokens.write_text('{"up": {}}')
    tokens.chmod(0o600)
    (tmp_path / "config-tools.json").write_text("{}")

    lines = migrate_legacy_state(local)

    assert len(lines) == 2
    moved = tmp_path / "state" / "tokens.json"
    assert moved.read_text() == '{"up": {}}'
    assert stat.S_IMODE(moved.stat().st_mode) == 0o600
    assert (tmp_path / "state" / "tools-cache.json").exists()
    assert not tokens.exists()
    assert migrate_legacy_state(local) == []


def test_legacy_state_never_overwrites_a_newer_one(tmp_path):
    local = tmp_path / "config.json"
    (tmp_path / "config-tokens.json").write_text("ancien")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "tokens.json").write_text("récent")
    lines = migrate_legacy_state(local)
    assert (tmp_path / "state" / "tokens.json").read_text() == "récent"
    assert (tmp_path / "config-tokens.json").read_text() == "ancien"
    assert "ignoré" in lines[0]
