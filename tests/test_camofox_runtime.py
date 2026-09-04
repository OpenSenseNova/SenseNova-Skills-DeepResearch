from pathlib import Path
from types import SimpleNamespace

from deepresearch_cli.camofox_runtime import CamofoxRuntime


def test_setup_installs_pinned_packages_and_engine_inside_cli_home(tmp_path, monkeypatch):
    home = tmp_path / "camofox"
    runtime = CamofoxRuntime(home=home)
    calls = []

    monkeypatch.setattr("shutil.which", lambda value: f"/usr/bin/{value}")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return SimpleNamespace(stdout="v22.23.1\n", returncode=0)
        if command[1] == "install":
            bins = home / "node_modules" / ".bin"
            bins.mkdir(parents=True)
            for name in ("camofox-browser", "camoufox-js"):
                path = bins / name
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o700)
            manifest = home / "node_modules" / "@askjo" / "camofox-browser" / "package.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"version":"1.14.0"}\n', encoding="utf-8")
        else:
            runtime.engine_dir.mkdir()
            (runtime.engine_dir / "version.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(runtime, "_health", lambda timeout=1.0: None)

    result = runtime.setup()

    install, kwargs = calls[1]
    assert "@askjo/camofox-browser@1.14.0" in install
    assert all("camofox-browser-mcp" not in item for item in install)
    assert kwargs["env"]["CAMOFOX_SKIP_DOWNLOAD"] == "1"
    fetch, fetch_kwargs = calls[2]
    assert fetch[-1] == "fetch"
    assert fetch_kwargs["env"]["CAMOUFOX_INSTALL_DIR"] == str(home / "engine")
    assert result["installed"] is True
    assert result["version"] == "1.14.0"
    assert result["version_matches"] is True
    assert result["engine_installed"] is True


def test_status_does_not_claim_an_unmanaged_install_is_running(tmp_path, monkeypatch):
    runtime = CamofoxRuntime(home=tmp_path / "missing")
    monkeypatch.setattr(runtime, "_health", lambda timeout=1.0: None)

    status = runtime.status()

    assert status["installed"] is False
    assert status["running"] is False
    assert status["managed_pid"] is None
