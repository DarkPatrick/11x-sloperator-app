from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.slack_files import attachment_prompt, download_slack_file


async def test_attachment_bytes_are_staged_with_safe_paths_and_no_token(tmp_path, monkeypatch):
    async def download(url, token, destination):
        assert url == "https://files.slack.com/private"
        assert token == "secret-token"
        destination.write_bytes(b"image-content")

    monkeypatch.setattr("sloperator.slack_files.download_slack_file", download)
    client = SimpleNamespace(
        token="secret-token",
        files_info=AsyncMock(
            return_value={
                "file": {
                    "name": "../../image.png",
                    "mimetype": "image/png",
                    "url_private_download": "https://files.slack.com/private",
                }
            }
        ),
    )
    prompt = await attachment_prompt(client, tmp_path, "C123", "1.2", [{"id": "F123"}])
    staged = list((tmp_path / "output" / "slack_inputs").rglob("*.png"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"image-content"
    assert str(staged[0]) in prompt
    assert "image-reading tool" in prompt
    assert "secret-token" not in prompt
    assert "https://files.slack.com/private" not in prompt


async def test_unavailable_attachment_does_not_drop_other_files(tmp_path, monkeypatch):
    client = SimpleNamespace(
        token="secret",
        files_info=AsyncMock(
            side_effect=[
                RuntimeError("private diagnostic"),
                {"file": {"name": "data.csv", "url_private": "https://files.slack.com/file"}},
            ]
        ),
    )

    async def download(url, token, destination):
        destination.write_text("a,b\n1,2\n")

    monkeypatch.setattr("sloperator.slack_files.download_slack_file", download)
    prompt = await attachment_prompt(client, tmp_path, "C123", "1.2", [{"id": "F1"}, {"id": "F2"}])
    assert "File unavailable" in prompt
    assert "data.csv" in prompt
    assert "private diagnostic" not in prompt


@pytest.mark.parametrize(
    "url",
    [
        "http://files.slack.com/private",
        "https://example.com/private",
        "https://files.slack.com.evil.test/private",
    ],
)
async def test_download_never_sends_slack_token_to_untrusted_host(tmp_path, url):
    with pytest.raises(ValueError, match="host"):
        await download_slack_file(url, "secret", tmp_path / "attachment")


@pytest.mark.parametrize("content_type", ["image/png", "text/html", "application/pdf"])
async def test_download_streams_bytes_and_cleans_up_oversized_file(
    tmp_path, monkeypatch, content_type
):
    class Response:
        status = 200
        content_length = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b"abc"
            yield b"def"

    Response.content_type = content_type

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"] == {"Authorization": "Bearer secret"}
            return Response()

    monkeypatch.setattr("sloperator.slack_files.aiohttp.ClientSession", Session)
    destination = tmp_path / "attachment.png"
    await download_slack_file("https://files.slack.com/file", "secret", destination)
    assert destination.read_bytes() == b"abcdef"
    assert destination.stat().st_mode & 0o777 == 0o600
    destination.unlink()
    monkeypatch.setattr("sloperator.slack_files.MAX_FILE_BYTES", 4)
    with pytest.raises(ValueError, match="50 MiB"):
        await download_slack_file("https://files.slack.com/file", "secret", destination)
    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))
