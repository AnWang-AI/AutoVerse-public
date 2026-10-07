from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from autoverse.frontends.web.files import (
    FileAccessError,
    list_directory_payload,
    raw_file_info,
    referenced_external_preview_paths,
    read_file_preview_payload,
    read_text_file_payload,
)
from autoverse.frontends.web.server import create_app

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdam\xfc"
    b"\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_lists_directory_entries_under_session_cwd(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "~").mkdir()
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    payload = list_directory_payload(tmp_path, ".", show_hidden=True)

    names = [entry["name"] for entry in payload["entries"]]
    assert names == ["src", ".env", "README.md"]
    assert payload["entries"][0]["is_dir"] is True
    assert payload["entries"][1]["path"] == ".env"


def test_reads_utf8_text_file(tmp_path: Path) -> None:
    (tmp_path / "docs" / "guides").mkdir(parents=True)
    (tmp_path / "docs" / "guides" / "using-webui.md").write_text(
        "# title\nbody\n",
        encoding="utf-8",
    )

    payload = read_text_file_payload(tmp_path, "docs/guides/using-webui.md")

    assert payload["path"] == "docs/guides/using-webui.md"
    assert payload["root"] == str(tmp_path)
    assert payload["language"] == "md"
    assert payload["lines"] == 2
    assert payload["content"] == "# title\nbody\n"


def test_file_preview_marks_markdown_kind(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# title\n", encoding="utf-8")

    payload = read_file_preview_payload(tmp_path, "README.md")

    assert payload["kind"] == "markdown"
    assert payload["language"] == "md"
    assert payload["content"] == "# title\n"


@pytest.mark.parametrize(
    ("name", "kind", "mime"),
    [
        ("chart.png", "image", "image/png"),
        ("photo.jpg", "image", "image/jpeg"),
        ("diagram.svg", "image", "image/svg+xml"),
        ("report.pdf", "pdf", "application/pdf"),
    ],
)
def test_file_preview_returns_browser_native_metadata(
    tmp_path: Path, name: str, kind: str, mime: str
) -> None:
    (tmp_path / name).write_bytes(b"not parsed here")

    payload = read_file_preview_payload(tmp_path, name)

    assert payload["kind"] == kind
    assert payload["path"] == name
    assert payload["root"] == str(tmp_path)
    assert payload["mime"] == mime
    assert payload["size"] == len(b"not parsed here")
    assert "content" not in payload


@pytest.mark.parametrize(("name", "language"), [("data.csv", "csv"), ("data.tsv", "tsv")])
def test_file_preview_marks_table_text(tmp_path: Path, name: str, language: str) -> None:
    (tmp_path / name).write_text("a,b\n1,2\n", encoding="utf-8")

    payload = read_file_preview_payload(tmp_path, name)

    assert payload["kind"] == "table"
    assert payload["language"] == language
    assert payload["content"] == "a,b\n1,2\n"


def test_file_preview_falls_back_to_raw_metadata_for_binary(tmp_path: Path) -> None:
    (tmp_path / "archive.bin").write_bytes(b"\x00\x01\x02")

    payload = read_file_preview_payload(tmp_path, "archive.bin")

    assert payload["kind"] == "raw"
    assert payload["root"] == str(tmp_path)
    assert payload["mime"] == "application/octet-stream"
    assert payload["size"] == 3
    assert "content" not in payload


def test_raw_file_info_rejects_paths_outside_session_cwd(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"image")

    with pytest.raises(FileAccessError) as err:
        raw_file_info(tmp_path, outside)

    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_file_preview_route_returns_image_metadata(tmp_path: Path) -> None:
    (tmp_path / "preview.png").write_bytes(_PNG_BYTES)
    app = create_app(cwd=str(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        sid = (await client.post("/api/sessions", json={"cwd": str(tmp_path)})).json()["session_id"]
        response = await client.get(f"/api/sessions/{sid}/file", params={"path": "preview.png"})

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "kind": "image",
        "path": "preview.png",
        "root": str(tmp_path),
        "name": "preview.png",
        "size": len(_PNG_BYTES),
        "mime": "image/png",
        "language": "png",
    }


@pytest.mark.asyncio
async def test_raw_file_route_streams_inline_bytes(tmp_path: Path) -> None:
    (tmp_path / "preview.png").write_bytes(_PNG_BYTES)
    app = create_app(cwd=str(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        sid = (await client.post("/api/sessions", json={"cwd": str(tmp_path)})).json()["session_id"]
        response = await client.get(
            f"/api/sessions/{sid}/file/raw", params={"path": "preview.png"}
        )

    assert response.status_code == 200
    assert response.content == _PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == 'inline; filename="preview.png"'
    assert response.headers["x-content-type-options"] == "nosniff"


def test_preview_reads_dot_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("hello\n", encoding="utf-8")

    payload = read_text_file_payload(tmp_path, "./docs/guide.md")

    assert payload["path"] == "docs/guide.md"
    assert payload["content"] == "hello\n"


@pytest.mark.parametrize(
    "raw_path",
    [
        "docs/guide.md:12",
        "docs/guide.md:12:3",
        "docs/guide.md#heading",
        "docs/guide.md?plain=1",
    ],
)
def test_preview_accepts_common_link_location_suffixes(
    tmp_path: Path, raw_path: str
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("hello\n", encoding="utf-8")

    payload = read_text_file_payload(tmp_path, raw_path)

    assert payload["path"] == "docs/guide.md"
    assert payload["content"] == "hello\n"


def test_rejects_paths_outside_session_cwd(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")

    with pytest.raises(FileAccessError) as err:
        read_text_file_payload(tmp_path, outside)

    assert err.value.status_code == 403


def test_reads_only_explicitly_authorized_external_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    referenced = tmp_path / "referenced.md"
    referenced.write_text("# referenced\n", encoding="utf-8")
    unreferenced = tmp_path / "unreferenced.md"
    unreferenced.write_text("# secret\n", encoding="utf-8")

    payload = read_text_file_payload(
        workspace,
        str(referenced),
        allowed_external_paths={referenced},
    )

    assert payload["path"] == str(referenced)
    assert payload["root"] == str(workspace)
    assert payload["content"] == "# referenced\n"
    with pytest.raises(FileAccessError) as err:
        read_text_file_payload(
            workspace,
            str(unreferenced),
            allowed_external_paths={referenced},
        )
    assert err.value.status_code == 403


def test_extracts_exact_external_paths_from_session_markdown_and_sidebar(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code_file = tmp_path / "notes with spaces.md"
    link_file = tmp_path / "diagram.png"
    changed_file = tmp_path / "changed.py"
    blocks = [
        {
            "block": {
                "kind": "markdown",
                "md": (
                    f"Open `{code_file}:12` or "
                    f"[the diagram]({link_file}). Ignore `normal` and `d → d/2`."
                ),
            }
        }
    ]

    paths = referenced_external_preview_paths(
        workspace,
        blocks,
        supplemental_paths=[str(changed_file)],
    )

    assert paths == frozenset({code_file, link_file, changed_file})


@pytest.mark.asyncio
async def test_file_preview_route_allows_referenced_external_but_not_arbitrary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    referenced = tmp_path / "referenced.md"
    referenced.write_text("# referenced\n", encoding="utf-8")
    unreferenced = tmp_path / "unreferenced.md"
    unreferenced.write_text("# secret\n", encoding="utf-8")

    class FakeSession:
        cwd = str(workspace)

        @staticmethod
        def external_file_preview_paths() -> frozenset[Path]:
            return frozenset({referenced})

    monkeypatch.setattr(
        "autoverse.frontends.web.server._existing",
        lambda _manager, _sid: FakeSession(),
    )
    app = create_app(cwd=str(workspace))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        allowed = await client.get(
            "/api/sessions/example/file", params={"path": str(referenced)}
        )
        denied = await client.get(
            "/api/sessions/example/file", params={"path": str(unreferenced)}
        )
        raw_allowed = await client.get(
            "/api/sessions/example/file/raw", params={"path": str(referenced)}
        )
        raw_denied = await client.get(
            "/api/sessions/example/file/raw", params={"path": str(unreferenced)}
        )

    assert allowed.status_code == 200
    assert allowed.json()["path"] == str(referenced)
    assert denied.status_code == 403
    assert raw_allowed.status_code == 200
    assert raw_allowed.content == b"# referenced\n"
    assert raw_denied.status_code == 403


def test_preview_location_suffix_still_rejects_outside_session_cwd(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")

    with pytest.raises(FileAccessError) as err:
        read_text_file_payload(tmp_path, "../outside.md:1")

    assert err.value.status_code == 403


def test_reads_dotenv_files_when_hidden_files_are_shown(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")

    listing = list_directory_payload(tmp_path, ".", show_hidden=True)

    assert [entry["name"] for entry in listing["entries"]] == [".env.local"]
    payload = read_text_file_payload(tmp_path, ".env.local")
    assert payload["content"] == "TOKEN=secret\n"
