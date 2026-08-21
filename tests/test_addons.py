"""Unit tests for the addons engine."""

import io
import json
import os
import urllib.error
import zipfile

import pytest

import vanilla_wow_launcher.core.config_store as config_store
import vanilla_wow_launcher.services.addons as addons


def test_is_allowed_git_url():
    assert addons.is_allowed_git_url("https://github.com/a/b")
    assert addons.is_allowed_git_url("https://gitlab.com/a/b")
    assert addons.is_allowed_git_url("https://codeberg.org/a/b")
    assert addons.is_allowed_git_url("https://octowow.st/git/a/b")
    assert addons.is_allowed_git_url("https://gitea.com/a/b")
    assert not addons.is_allowed_git_url("http://github.com/a/b")
    assert not addons.is_allowed_git_url("https://evil.com/a/b")
    assert not addons.is_allowed_git_url("https://github.com.evil.com/a/b")
    assert not addons.is_allowed_git_url("not a url")


def test_git_parts_octowow_gitea():
    """OctoWoW-hosted repos are Gitea, API at <host>/git/api/v1."""
    kind, repo_url, owner, repo, api = addons._git_parts(
        "https://octowow.st/git/octocontr/OctoWoW"
    )
    assert kind == "gitea"
    assert (owner, repo) == ("octocontr", "OctoWoW")
    assert repo_url == "https://octowow.st/git/octocontr/OctoWoW"
    assert api == "https://octowow.st/git/api/v1"


def test_addon_zip_url_octowow_gitea():
    url = addons.addon_zip_url("https://octowow.st/git/a/b", "abc123" * 6)
    assert (
        url
        == "https://octowow.st/git/a/b/archive/abc123abc123abc123abc123abc123abc123.zip"
    )


def test_git_parts_github():
    kind, repo_url, owner, repo, api = addons._git_parts(
        "https://github.com/Otari98/_LazyPig"
    )
    assert kind == "github"
    assert owner == "Otari98"
    assert repo == "_LazyPig"
    assert api == "https://api.github.com"


def test_git_parts_strips_git_suffix():
    _k, repo_url, owner, repo, _api = addons._git_parts(
        "https://github.com/a/repo.git"
    )
    assert (owner, repo) == ("a", "repo")


def test_git_parts_gitlab():
    kind, repo_url, _o, _r, api = addons._git_parts("https://gitlab.com/a/b")
    assert kind == "gitlab"
    assert api == "https://gitlab.com/api/v4"


def test_addon_zip_url_github():
    url = addons.addon_zip_url("https://github.com/a/b", "abc123" * 6)
    assert (
        url
        == "https://github.com/a/b/archive/abc123abc123abc123abc123abc123abc123.zip"
    )


def test_addon_zip_url_gitlab():
    url = addons.addon_zip_url("https://gitlab.com/a/b", "abc123")
    assert url == "https://gitlab.com/a/b/-/archive/abc123/b-abc123.zip"


def test_read_toc_file(tmp_path):
    toc_path = tmp_path / "x.toc"
    toc_path.write_text(
        "## Title: My Addon\n## Notes: Great\n## Interface: 11400\n",
        encoding="utf-8",
    )
    toc = addons.read_toc_file(str(toc_path))
    assert toc["Title"] == "My Addon"
    assert toc["Interface"] == "11400"


def test_read_toc_file_missing(tmp_path):
    assert addons.read_toc_file(str(tmp_path / "nope.toc")) == {}


def test_fetch_addons_catalog_cached(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    catalog = [
        {
            "name": "pfUI",
            "git": "https://github.com/brues-code/pfUI",
            "toc": {"Title": "pfUI", "Notes": "n", "Extra": "skip"},
        }
    ]
    config_store.save_config(
        {"addons_catalog_cache": {"timestamp": 9999999999, "catalog": catalog}}
    )

    def fail(*a, **k):
        raise AssertionError("cached catalog must not hit the network")

    monkeypatch.setattr(addons, "secure_urlopen", fail)
    assert addons.fetch_addons_catalog() == catalog


def test_fetch_addons_catalog_slims_and_stores(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    raw = [
        {
            "name": "pfUI",
            "git": "https://github.com/brues-code/pfUI",
            "branch": "master",
            "ref": None,
            "description": "d",
            "toc": {"Title": "pfUI", "Notes": "n", "Extra": "skip"},
        }
    ]
    payload = json.dumps(raw).encode()
    monkeypatch.setattr(
        addons,
        "secure_urlopen",
        lambda *a, **k: type(
            "R",
            (),
            {
                "__enter__": lambda s: s,
                "__exit__": lambda *x: False,
                "read": lambda s=0: payload,
            },
        )(),
    )

    out = addons.fetch_addons_catalog()
    assert out[0]["toc"] == {"Title": "pfUI", "Notes": "n"}
    assert "Extra" not in out[0]["toc"]


def test_fetch_addons_catalog_keeps_flags(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    raw = [
        {
            "name": "A",
            "git": "https://github.com/x/A",
            "recommended": True,
            "blocked": False,
        }
    ]
    payload = json.dumps(raw).encode()
    monkeypatch.setattr(
        addons,
        "secure_urlopen",
        lambda *a, **k: type(
            "R",
            (),
            {
                "__enter__": lambda s: s,
                "__exit__": lambda *x: False,
                "read": lambda s=0: payload,
            },
        )(),
    )

    out = addons.fetch_addons_catalog()
    assert out[0]["recommended"] is True
    assert out[0]["blocked"] is False


def test_fetch_addons_catalog_drops_disallowed_git(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    raw = [{"name": "A", "git": "https://evil.com/x"}]
    payload = json.dumps(raw).encode()
    monkeypatch.setattr(
        addons,
        "secure_urlopen",
        lambda *a, **k: type(
            "R",
            (),
            {
                "__enter__": lambda s: s,
                "__exit__": lambda *x: False,
                "read": lambda s=0: payload,
            },
        )(),
    )

    assert addons.fetch_addons_catalog() == []


# ── multiple registries (ordered override) ───────────────────────────────


def _fake_response(payload):
    return type(
        "R",
        (),
        {
            "__enter__": lambda s: s,
            "__exit__": lambda *x: False,
            "read": lambda s=0: payload,
        },
    )


def _configure_urls(urls):
    from vanilla_wow_launcher.core import launcher

    launcher.configure_from_dict(
        {
            "server": {
                "base_url": "https://launcher.test",
                "addons_registry_urls": list(urls),
            }
        }
    )


def test_fetch_addons_catalog_merges_registries_in_order(
    tmp_path, monkeypatch
):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    _configure_urls(
        ["https://a.test/official.json", "https://b.test/overrides.json"]
    )
    by_url = {
        "https://a.test/official.json": [
            {
                "name": "pfUI",
                "git": "https://github.com/brues-code/pfUI",
                "branch": "master",
            },
            {"name": "OnlyA", "git": "https://github.com/a/OnlyA"},
        ],
        "https://b.test/overrides.json": [
            {
                "name": "pfUI",
                "git": "https://github.com/roby-brok/pfUI",
                "branch": "master",
                "description": "OctoWoW fork",
            },
        ],
    }

    def fake(req, *a, **k):
        return _fake_response(json.dumps(by_url[req.full_url]).encode())()

    monkeypatch.setattr(addons, "secure_urlopen", fake)

    out = addons.fetch_addons_catalog()
    by_name = {a["name"]: a for a in out}
    assert len(out) == 2
    # The later registry overrides the earlier one for pfUI.
    assert by_name["pfUI"]["git"] == "https://github.com/roby-brok/pfUI"
    assert by_name["pfUI"]["branch"] == "master"
    # Entries only present in the first registry survive.
    assert by_name["OnlyA"]["git"] == "https://github.com/a/OnlyA"


def test_fetch_addons_catalog_failed_registry_uses_its_cache(
    tmp_path, monkeypatch
):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    _configure_urls(
        ["https://a.test/official.json", "https://b.test/overrides.json"]
    )
    config_store.save_config(
        {
            "addons_catalog_cache": {
                "https://a.test/official.json": {
                    "timestamp": 9999999999,
                    "catalog": [
                        {
                            "name": "pfUI",
                            "git": "https://github.com/brues-code/pfUI",
                        }
                    ],
                },
                "https://b.test/overrides.json": {
                    "timestamp": 9999999999,
                    "catalog": [
                        {
                            "name": "pfUI",
                            "git": "https://github.com/roby-brok/pfUI",
                        }
                    ],
                },
            }
        }
    )

    def boom(*a, **k):
        raise AssertionError("cached catalogs must not hit the network")

    monkeypatch.setattr(addons, "secure_urlopen", boom)

    out = addons.fetch_addons_catalog()
    pfui = next(a for a in out if a["name"] == "pfUI")
    assert pfui["git"] == "https://github.com/roby-brok/pfUI"


def test_registry_urls_override_replaces_launcher_list(tmp_path):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    _configure_urls(["https://a.test/1.json", "https://b.test/2.json"])
    assert addons.registry_urls() == [
        "https://a.test/1.json",
        "https://b.test/2.json",
    ]

    addons.set_registry_url("https://c.test/custom.json")
    assert addons.registry_urls() == ["https://c.test/custom.json"]
    assert addons.registry_url() == "https://c.test/custom.json"

    addons.reset_registry_url()
    assert addons.registry_urls() == [
        "https://a.test/1.json",
        "https://b.test/2.json",
    ]


def test_catalog_from_cache_merges_in_configured_order(tmp_path):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    _configure_urls(
        ["https://a.test/official.json", "https://b.test/overrides.json"]
    )
    config_store.save_config(
        {
            "addons_catalog_cache": {
                "https://a.test/official.json": {
                    "timestamp": 1,
                    "catalog": [
                        {
                            "name": "pfUI",
                            "git": "https://github.com/brues-code/pfUI",
                            "branch": None,
                            "ref": None,
                        }
                    ],
                },
                "https://b.test/overrides.json": {
                    "timestamp": 1,
                    "catalog": [
                        {
                            "name": "pfUI",
                            "git": "https://github.com/roby-brok/pfUI",
                            "branch": "master",
                            "ref": None,
                            "description": "fork",
                        }
                    ],
                },
            }
        }
    )

    out = addons.catalog_from_cache()
    pfui = next(a for a in out if a["name"] == "pfUI")
    assert pfui["git"] == "https://github.com/roby-brok/pfUI"
    assert pfui["branch"] == "master"


def test_example_octowow_addons_overrides_validate():
    """The bundled override list must pass the addon validator so the
    launcher config's addons_registry_urls stays usable."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "examples", "octowow_addons.json"
    )
    raw = json.load(open(path, encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) >= 1
    for entry in raw:
        cleaned = addons._custom_validator(entry)
        assert cleaned is not None, entry
    assert any(
        a["name"] == "pfUI" for a in (addons._custom_validator(e) for e in raw)
    )


def test_addons_catalog_merges_custom(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    monkeypatch.setattr(
        addons,
        "fetch_addons_catalog",
        lambda force=False: [
            {
                "name": "A",
                "git": "https://github.com/x/A",
                "branch": None,
                "ref": None,
                "description": None,
                "toc": {},
                "recommended": False,
                "blocked": False,
            }
        ],
    )
    monkeypatch.setattr(
        addons.catalog,
        "custom_file",
        lambda kind: str(tmp_path / "custom.json"),
    )
    (tmp_path / "custom.json").write_text(
        '[{"folder": "A", "git": "https://github.com/fork/A", '
        '"recommended": true}]',
        encoding="utf-8",
    )

    merged = {a["name"]: a for a in addons.addons_catalog()}
    assert merged["A"]["git"] == "https://github.com/fork/A"
    assert merged["A"]["recommended"] is True


def test_addon_remote_sha_cached(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    key = "https://github.com/a/b#"
    config_store.save_config(
        {"addon_sha_cache": {key: {"timestamp": 9999999999, "sha": "f" * 40}}}
    )

    def fail(*a, **k):
        raise AssertionError("cached sha must not hit the network")

    monkeypatch.setattr(addons, "_api_json", fail)
    assert addons.addon_remote_sha("https://github.com/a/b") == "f" * 40


def test_addon_remote_sha_resolves_and_caches(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    sha = "abcd" * 10

    def fake_api_json(url, timeout=10):
        return {"sha": sha} if "/commits/" in url else [{"sha": sha}]

    monkeypatch.setattr(addons, "_api_json", fake_api_json)
    assert addons.addon_remote_sha("https://github.com/a/b") == sha
    assert addons.addon_cached_sha("https://github.com/a/b") == sha


def test_addon_remote_sha_gates_disallowed_host(tmp_path, monkeypatch):
    """A git URL outside the allowlist short-circuits to None before any
    API call or `git ls-remote` subprocess (review §12.3)."""

    def boom(*a, **k):
        raise AssertionError("disallowed host must not be contacted")

    monkeypatch.setattr(addons, "secure_urlopen", boom)
    monkeypatch.setattr(addons.subprocess, "run", boom)

    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    assert addons.addon_remote_sha("https://evil.example/org/repo.git") is None


def test_addon_remote_sha_allows_allowlisted_host(tmp_path, monkeypatch):
    """An allowlisted host proceeds normally: the mocked API sha is
    resolved and cached."""
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    sha = "abcd" * 10

    def fake_api_json(url, timeout=10):
        return {"sha": sha} if "/commits/" in url else [{"sha": sha}]

    monkeypatch.setattr(addons, "_api_json", fake_api_json)
    assert addons.addon_remote_sha("https://github.com/a/b.git") == sha
    assert addons.addon_cached_sha("https://github.com/a/b.git") == sha


# ── git ls-remote fallback ────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_git_ls_remote_parses_head(monkeypatch):
    proc = _FakeProc(
        stdout=("ab" * 20 + "\tHEAD\n" + "cd" * 20 + "\trefs/heads/main\n")
    )
    calls = []
    monkeypatch.setattr(
        addons.subprocess, "run", lambda *a, **k: calls.append((a, k)) or proc
    )
    assert (
        addons._git_ls_remote_sha("https://github.com/a/b", None) == "ab" * 20
    )
    args, kwargs = calls[0]
    assert args[0] == ["git", "ls-remote", "https://github.com/a/b", "HEAD"]
    assert kwargs.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0"


def test_git_ls_remote_parses_branch_and_tag(monkeypatch):
    proc = _FakeProc(
        stdout=(
            "ab" * 20
            + "\trefs/heads/main\n"
            + "cd" * 20
            + "\trefs/tags/v1.0\n"
        )
    )
    monkeypatch.setattr(addons.subprocess, "run", lambda *a, **k: proc)
    assert (
        addons._git_ls_remote_sha("https://github.com/a/b", "main")
        == "ab" * 20
    )
    assert (
        addons._git_ls_remote_sha("https://github.com/a/b", "v1.0")
        == "cd" * 20
    )


def test_git_ls_remote_matches_short_ref(monkeypatch):
    proc = _FakeProc(stdout="ef" * 20 + "\trefs/remotes/origin/release\n")
    monkeypatch.setattr(addons.subprocess, "run", lambda *a, **k: proc)
    assert (
        addons._git_ls_remote_sha("https://github.com/a/b", "release")
        == "ef" * 20
    )


def test_git_ls_remote_missing_git_returns_none(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(addons.subprocess, "run", boom)
    assert addons._git_ls_remote_sha("https://github.com/a/b", None) is None


def test_git_ls_remote_failure_returns_none(monkeypatch):
    monkeypatch.setattr(
        addons.subprocess, "run", lambda *a, **k: _FakeProc(returncode=2)
    )
    assert addons._git_ls_remote_sha("https://github.com/a/b", "main") is None
    monkeypatch.setattr(
        addons.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            addons.subprocess.TimeoutExpired("git", 15)
        ),
    )
    assert addons._git_ls_remote_sha("https://github.com/a/b", None) is None


def test_addon_remote_sha_falls_back_to_git_ls_remote_on_api_error(
    tmp_path, monkeypatch
):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})
    sha = "beef" * 10

    def api_boom(url, timeout=10):
        raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)

    monkeypatch.setattr(addons, "_api_json", api_boom)
    monkeypatch.setattr(addons, "_git_ls_remote_sha", lambda url, pin: sha)
    assert addons.addon_remote_sha("https://github.com/a/b") == sha
    assert addons.addon_cached_sha("https://github.com/a/b") == sha


def test_addon_remote_sha_raises_when_both_paths_fail(tmp_path, monkeypatch):
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})

    def api_boom(url, timeout=10):
        raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)

    monkeypatch.setattr(addons, "_api_json", api_boom)
    monkeypatch.setattr(addons, "_git_ls_remote_sha", lambda url, pin: None)
    with pytest.raises(RuntimeError, match="rate limit"):
        addons.addon_remote_sha("https://github.com/a/b", raise_errors=True)


def test_addon_remote_sha_logs_cause_when_unresolvable(tmp_path, monkeypatch):
    """A failed resolve returns None but still logs a dim diagnostic line so
    a wall of 'Couldn't check' isn't a silent mystery."""
    config_store.configure(
        str(tmp_path / "config.json"), str(tmp_path / "cache.json")
    )
    config_store.save_config({})

    def api_boom(url, timeout=10):
        raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)

    monkeypatch.setattr(addons, "_api_json", api_boom)
    monkeypatch.setattr(addons, "_git_ls_remote_sha", lambda url, pin: None)
    logged = []
    monkeypatch.setattr(
        addons, "log", lambda msg, tag="": logged.append((msg, tag))
    )

    assert addons.addon_remote_sha("https://github.com/a/b") is None

    assert logged
    msg, tag = logged[0]
    assert tag == "dim"
    assert "rate limit" in msg


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_install_addon_files_extracts(tmp_path, monkeypatch):
    client = tmp_path / "client"
    payload = _zip_bytes(
        {
            "pfUI-master/pfUI.toc": "## Title: pfUI\n",
            "pfUI-master/lib/x.lua": "-- lib",
        }
    )
    monkeypatch.setattr(
        addons,
        "secure_urlopen",
        lambda *a, **k: type(
            "R",
            (),
            {
                "__enter__": lambda s: s,
                "__exit__": lambda *x: False,
                "read": lambda s=0: payload,
            },
        )(),
    )

    addons.install_addon_files(
        str(client), "pfUI", "https://github.com/brues-code/pfUI", "abcd" * 10
    )
    base = client / "Interface" / "AddOns" / "pfUI"
    assert (base / "pfUI.toc").exists()
    assert (base / "lib" / "x.lua").exists()
    assert not (client / "Interface" / "AddOns" / "pfUI.tmp_install").exists()


def test_install_addon_files_path_traversal_safe(tmp_path, monkeypatch):
    client = tmp_path / "client"
    payload = _zip_bytes(
        {
            "x-master/ok.txt": "ok",
            "x-master/../../escape.txt": "evil",
        }
    )
    monkeypatch.setattr(
        addons,
        "secure_urlopen",
        lambda *a, **k: type(
            "R",
            (),
            {
                "__enter__": lambda s: s,
                "__exit__": lambda *x: False,
                "read": lambda s=0: payload,
            },
        )(),
    )

    addons.install_addon_files(
        str(client), "x", "https://github.com/a/x", "abcd" * 10
    )
    assert (client / "Interface" / "AddOns" / "x" / "ok.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_patch_pfui_installs_profile(tmp_path):
    client = tmp_path / "client"
    base = client / "Interface" / "AddOns" / "pfUI"
    (base / "env").mkdir(parents=True)
    (base / "modules").mkdir(parents=True)
    (base / "env" / "profiles.lua").write_text(
        "pfUI_profiles = {}\n", encoding="utf-8"
    )

    addons.patch_pfui_default_profile(str(client))
    content = (base / "env" / "profiles.lua").read_text(encoding="utf-8")
    assert addons._PFUI_MARK_BEGIN in content
    assert 'pfUI_profiles["Default"]' in content

    # Idempotent: re-applying must not duplicate the block.
    addons.patch_pfui_default_profile(str(client))
    content2 = (base / "env" / "profiles.lua").read_text(encoding="utf-8")
    assert content2.count(addons._PFUI_MARK_BEGIN) == 1


def test_patch_pfui_missing_profile_returns_gracefully(tmp_path):
    client = tmp_path / "client"
    addons.patch_pfui_default_profile(str(client))  # no pfUI installed
