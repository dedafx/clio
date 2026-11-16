"""The content cache: file bytes for display, named by digest and size."""

from __future__ import annotations

import os
import stat

import pytest

from deda import clio
from deda.clio.content import ContentCache, ContentRequest, requests_from_revisions


@pytest.fixture
def alice(make_session):
    return make_session()


def _save(session, files: dict[str, str | bytes], message: str = "save") -> None:
    for relative, content in files.items():
        path = session.paths.local_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    session.workspace.save(message=message)


def _requests(session, relative: str):
    return requests_from_revisions(session.history.revisions(relative, limit=50).items)


def test_a_revision_is_fetched_once_and_named_by_its_content(alice, tmp_path):
    _save(alice, {"a.bin": b"\x00\x01version one"})
    _save(alice, {"a.bin": b"\x00\x01version two"})
    cache = ContentCache(tmp_path / "content")
    newest, oldest = _requests(alice, "a.bin")

    before = alice.backend.calls["print"]
    one = cache.get(alice, oldest)
    assert one.read_bytes() == b"\x00\x01version one"
    assert one.name == f"{oldest.digest.lower()}-{oldest.size}.bin"
    # Read-only by mode: os.access(W_OK) is always true for root.
    assert not one.stat().st_mode & stat.S_IWRITE
    assert cache.get(alice, oldest) == one
    assert alice.backend.calls["print"] == before + 1  # the second call used the cache

    assert cache.get(alice, newest).read_bytes() == b"\x00\x01version two"
    assert cache.total_bytes == len(b"\x00\x01version one") + len(b"\x00\x01version two")


def test_identical_content_is_stored_once(alice, tmp_path):
    _save(alice, {"a.bin": b"\x00same bytes", "copy/b.bin": b"\x00same bytes"})
    cache = ContentCache(tmp_path / "content")
    [a] = _requests(alice, "a.bin")
    [b] = _requests(alice, "copy/b.bin")

    first = cache.get(alice, a)
    prints = alice.backend.calls["print"]
    second = cache.get(alice, b)

    assert first == second  # same digest and size, same extension: one file
    assert alice.backend.calls["print"] == prints  # the same content is never downloaded twice
    assert len(list((tmp_path / "content").iterdir())) == 1


def test_text_files_check_out_whatever_the_line_endings(alice, tmp_path):
    _save(alice, {"a.usda": "#usda 1.0\nline two\n"})
    cache = ContentCache(tmp_path / "content")
    [request] = _requests(alice, "a.usda")
    assert request.is_text
    path = cache.get(alice, request)
    assert path.read_bytes().replace(b"\r\n", b"\n") == b"#usda 1.0\nline two\n"
    assert cache.verify().removed == ()


def test_the_least_recently_used_files_go_first(alice, tmp_path):
    _save(alice, {f"f{i}.bin": bytes([0, i]) * 500 for i in range(4)})  # 1000 bytes each
    requests = {r.depot_path.rsplit("/", 1)[1]: r for i in range(4) for r in _requests(alice, f"f{i}.bin")}
    cache = ContentCache(tmp_path / "content", max_bytes=2500)

    paths = {}
    for name in ("f0.bin", "f1.bin"):
        paths[name] = cache.get(alice, requests[name])
        os.utime(paths[name], (1000 + len(paths), 1000 + len(paths)))  # f0 oldest, f1 newer
    cache.get(alice, requests["f0.bin"])  # use f0 again: now it is the newest
    os.utime(paths["f0.bin"], (5000, 5000))
    cache.get(alice, requests["f2.bin"])  # 3000 bytes > 2500: evict down to 2250 -> f1 (oldest) goes

    assert paths["f0.bin"].exists() and not paths["f1.bin"].exists()
    assert cache.contains(requests["f2.bin"])
    assert cache.total_bytes <= 2500


def test_a_revision_that_is_gone_is_an_error_for_that_revision_only(alice, tmp_path):
    _save(alice, {"a.bin": b"\x00a", "b.bin": b"\x00b"})
    cache = ContentCache(tmp_path / "content")
    [a] = _requests(alice, "a.bin")
    [b] = _requests(alice, "b.bin")
    gone = ContentRequest(a.depot_path, 99, a.digest, a.size)

    with pytest.raises(clio.WorkflowError):
        cache.get(alice, gone)
    results = cache.get_many(alice, [a, gone, b, a])

    assert isinstance(results[gone], clio.WorkflowError)
    assert results[a].read_bytes() == b"\x00a" and results[b].read_bytes() == b"\x00b"
    assert len(results) == 3  # duplicates answered once
    assert not list((tmp_path / "content").glob("*.tmp"))  # nothing left behind


def test_a_wrong_digest_is_refused(alice, tmp_path):
    _save(alice, {"a.bin": b"\x00a"})
    cache = ContentCache(tmp_path / "content")
    [a] = _requests(alice, "a.bin")
    wrong = ContentRequest(a.depot_path, a.rev, "0" * 32, a.size)
    with pytest.raises(clio.WorkflowError, match="another digest"):
        cache.get(alice, wrong)
    assert not cache.contains(wrong)


def test_clear_and_verify(alice, tmp_path):
    _save(alice, {"a.bin": b"\x00a", "b.bin": b"\x00b"})
    cache = ContentCache(tmp_path / "content")
    requests = [r for name in ("a.bin", "b.bin") for r in _requests(alice, name)]
    paths = [cache.get(alice, r) for r in requests]

    paths[0].chmod(0o666)
    paths[0].write_bytes(b"damaged")
    report = cache.verify()
    assert report.checked == 2 and report.removed == (paths[0].name,)
    assert not paths[0].exists() and paths[1].exists()

    assert cache.clear() == len(b"\x00b")
    assert cache.total_bytes == 0 and list((tmp_path / "content").iterdir()) == []


def test_the_session_has_a_cache_in_the_clio_cache_folder(alice, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", os.fspath(tmp_path / "local"))
    monkeypatch.setenv("XDG_CACHE_HOME", os.fspath(tmp_path / "xdg"))
    session = clio.connect(content_cache_mb=7)
    assert session.content.max_bytes == 7 * 2**20
    assert session.content.folder.name == "content" and session.content.folder.parent.name == "clio"


def test_history_verify_drops_entries_the_server_no_longer_has(alice):
    _save(alice, {"a.bin": b"\x00one"})
    _save(alice, {"a.bin": b"\x00two"})
    _save(alice, {"b.bin": b"\x00b"})
    alice.history.revisions("a.bin")
    alice.history.revisions("b.bin")
    assert alice.history.verify() == 0  # all as the server has them

    depot_a = alice.paths.depot_path("a.bin")
    entry = alice.history.cache.files[depot_a]
    entry.items.append(_fake_revision(entry.items[0], 7))  # a revision the server never had

    assert alice.history.verify("a.bin") == 1
    assert depot_a not in alice.history.cache.files
    assert alice.paths.depot_path("b.bin") in alice.history.cache.files  # other entries are kept


def _fake_revision(template, rev: int):
    from dataclasses import replace

    return replace(template, rev=rev)


def test_cli_cache_clear_and_verify(alice, p4_server, monkeypatch, tmp_path):
    from click.testing import CliRunner

    from deda.clio.cli import cli

    monkeypatch.setenv("LOCALAPPDATA", os.fspath(tmp_path / "local"))
    monkeypatch.setenv("XDG_CACHE_HOME", os.fspath(tmp_path / "xdg"))
    monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
    monkeypatch.chdir(alice.paths.root)
    _save(alice, {"a.bin": b"\x00one"})
    base = ["--port", p4_server.port, "--user", alice.user, "--client", alice.client]
    runner = CliRunner()

    shown = runner.invoke(cli, [*base, "cache", "verify"], catch_exceptions=False)
    assert shown.exit_code == 0 and "0 entry(ies) dropped" in shown.output
    cleared = runner.invoke(cli, [*base, "cache", "clear"], catch_exceptions=False)
    assert cleared.exit_code == 0 and cleared.output.startswith("Cleared the cache")
