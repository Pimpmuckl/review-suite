import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.paths import (
    _git_top_level,
    cwd_path_from_normalized,
    normalize_cwd,
    resolve_cd_path,
    resolve_repo_root,
)


def test_normalize_cwd_maps_wsl_unc_to_stable_key() -> None:
    assert (
        normalize_cwd(r"\\wsl.localhost\Ubuntu\home\alice\code\repo")
        == "wsl:ubuntu:/home/alice/code/repo"
    )
    assert (
        normalize_cwd("//wsl$/Ubuntu/home/alice/code/repo")
        == "wsl:ubuntu:/home/alice/code/repo"
    )
    assert (
        normalize_cwd("wsl:Ubuntu:/home/alice/code/repo")
        == "wsl:ubuntu:/home/alice/code/repo"
    )


def test_normalize_cwd_maps_native_wsl_path_to_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    assert normalize_cwd("/home/alice/code/repo") == "wsl:ubuntu:/home/alice/code/repo"


def test_cwd_path_from_normalized_returns_native_wsl_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    assert (
        cwd_path_from_normalized("wsl:ubuntu:/home/alice/code/repo").as_posix()
        == "/home/alice/code/repo"
    )


def test_resolve_repo_root_autotranslates_unique_wsl_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "review_suite_core.paths._list_wsl_distributions", lambda: ["Ubuntu"]
    )
    monkeypatch.setattr(
        "review_suite_core.paths._path_exists",
        lambda path_text: (
            path_text == "//wsl.localhost/Ubuntu/home/alice/code/sample-web-wt-alpha"
        ),
    )
    monkeypatch.setattr(
        "review_suite_core.paths.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout="//wsl.localhost/Ubuntu/home/alice/code/sample-web-wt-alpha\n",
            stderr="",
        ),
    )

    resolved = resolve_repo_root("/home/alice/code/sample-web-wt-alpha")

    assert (
        str(resolved).replace("\\", "/")
        == "//wsl.localhost/Ubuntu/home/alice/code/sample-web-wt-alpha"
    )


def test_resolve_repo_root_autotranslates_mnt_drive_to_windows_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("review_suite_core.paths._list_wsl_distributions", lambda: [])
    monkeypatch.setattr(
        "review_suite_core.paths._path_exists",
        lambda path_text: path_text == "C:/Code/sample-repo",
    )
    monkeypatch.setattr(
        "review_suite_core.paths.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="C:/Code/sample-repo\n", stderr=""
        ),
    )

    resolved = resolve_repo_root("/mnt/c/Code/sample-repo")

    assert str(resolved).replace("\\", "/") == "C:/Code/sample-repo"


def test_resolve_cd_path_autotranslates_mnt_drive_without_git_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("review_suite_core.paths._list_wsl_distributions", lambda: [])
    monkeypatch.setattr(
        "review_suite_core.paths._path_exists",
        lambda path_text: path_text == "C:/Code/sample-repo",
    )

    resolved = resolve_cd_path("/mnt/c/Code/sample-repo")

    assert str(resolved).replace("\\", "/") == "C:/Code/sample-repo"


def test_resolve_repo_root_prefers_native_drive_translation_when_unc_mirrors_also_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "review_suite_core.paths._list_wsl_distributions", lambda: ["Ubuntu", "Debian"]
    )
    monkeypatch.setattr(
        "review_suite_core.paths._path_exists",
        lambda path_text: (
            path_text
            in {
                "C:/Code/sample-repo",
                "//wsl.localhost/Ubuntu/mnt/c/Code/sample-repo",
                "//wsl.localhost/Debian/mnt/c/Code/sample-repo",
            }
        ),
    )
    monkeypatch.setattr(
        "review_suite_core.paths.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="C:/Code/sample-repo\n", stderr=""
        ),
    )

    resolved = resolve_repo_root("/mnt/c/Code/sample-repo")

    assert str(resolved).replace("\\", "/") == "C:/Code/sample-repo"


def test_resolve_repo_root_rejects_unresolved_posix_wsl_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "review_suite_core.paths._list_wsl_distributions", lambda: ["Ubuntu"]
    )
    monkeypatch.setattr("review_suite_core.paths._path_exists", lambda path_text: False)

    with pytest.raises(
        ValueError, match="received a POSIX-style repo path on Windows"
    ) as excinfo:
        resolve_repo_root("/home/alice/code/sample-web-wt-alpha")

    message = str(excinfo.value)
    assert (
        "--cd //wsl.localhost/<Distro>/home/alice/code/sample-web-wt-alpha" in message
    )
    assert "C:/Code/your-repo" in message


def test_resolve_repo_root_returns_git_top_level_for_subdir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src" / "feature"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    resolved = resolve_repo_root(str(nested))

    assert resolved == repo.resolve()


def test_git_top_level_preserves_foreign_style_git_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "review_suite_core.paths.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="C:/Code/sample-repo\n", stderr=""
        ),
    )

    resolved = _git_top_level(Path("ignored"))

    assert str(resolved).replace("\\", "/") == "C:/Code/sample-repo"
