"""``export`` for floors, labels, areas, devices and entities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from hass_atlas.atlas import export_cmd, yamlio
from hass_atlas.atlas.yamlio import AtlasFormatError, load_atlas
from hass_atlas.cli import cli
from hass_atlas.context import Context
from hass_atlas.ha_client import HAClientError
from tests.atlas.fake_ha import FIXTURES, FakeHA, load_fixture

GOLDEN = FIXTURES / "golden"
READS = ("/list", "/get_entries")
FILES = (
    "atlas.yaml",
    "floors.yaml",
    "labels.yaml",
    "areas.yaml",
    "devices.yaml",
    "entities.yaml",
)


@pytest.fixture(autouse=True)
def _version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export_cmd, "_package_version", lambda: "0.0.0")


def run_export(
    monkeypatch: pytest.MonkeyPatch, fake: FakeHA, *args: str, dry_run: bool = False
) -> Result:
    monkeypatch.setattr(Context, "client", lambda self: fake)
    pre = ["--dry-run"] if dry_run else []
    return CliRunner().invoke(
        cli, ["--url", "http://ha.test:8123", "--token", "t", *pre, "export", *args]
    )


def snapshot(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(path.iterdir())}


def test_golden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    out = tmp_path / "atlas"
    result = run_export(monkeypatch, fake, "--out", str(out))
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == sorted(FILES)
    for name in FILES:
        assert (out / name).read_text(encoding="utf-8") == (GOLDEN / name).read_text(
            encoding="utf-8"
        ), name
    assert all(cmd.endswith(READS) for cmd, _ in fake.sent)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["atlas"]


def test_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    assert run_export(monkeypatch, FakeHA(), "--out", str(first)).exit_code == 0

    data = load_fixture()
    data["labels"].reverse()
    for area in data["areas"]:
        area["labels"].reverse()
        area["aliases"].reverse()
    for floor in data["floors"]:
        floor["aliases"].reverse()
        floor["modified_at"] += 1000
    data["devices"].reverse()
    for device in data["devices"]:
        device["identifiers"].reverse()
        device["labels"].reverse()
        device["modified_at"] += 1000
    assert run_export(monkeypatch, FakeHA(data), "--out", str(second)).exit_code == 0
    assert snapshot(first) == snapshot(second)

    assert run_export(monkeypatch, FakeHA(), "--out", str(first)).exit_code == 0
    assert snapshot(first) == snapshot(second)


def test_registry_order_kept_for_floors_and_areas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    data["areas"].reverse()
    data["floors"].reverse()
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(data), "--out", str(out)).exit_code == 0
    atlas = load_atlas(out)
    assert [a["area_id"] for a in atlas.docs["areas"]] == [a["area_id"] for a in data["areas"]]
    assert [f["floor_id"] for f in atlas.docs["floors"]] == [f["floor_id"] for f in data["floors"]]


def test_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    out = tmp_path / "atlas"
    result = run_export(monkeypatch, fake, "--out", str(out), "--only", "labels")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == ["atlas.yaml", "labels.yaml"]
    assert [cmd for cmd, _ in fake.sent] == ["config/label_registry/list"]
    assert load_atlas(out).manifest["domains"] == ["labels"]


def test_only_keeps_other_domains(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    (out / "notes.txt").write_text("kept\n", encoding="utf-8")
    before = snapshot(out)

    data = load_fixture()
    data["floors"][0]["name"] = "Top"
    fake = FakeHA(data)
    result = run_export(monkeypatch, fake, "--out", str(out), "--only", "floors, areas")
    assert result.exit_code == 0, result.output
    assert {cmd for cmd, _ in fake.sent} == {
        "config/floor_registry/list",
        "config/area_registry/list",
    }
    after = snapshot(out)
    assert after["labels.yaml"] == before["labels.yaml"]
    assert after["notes.txt"] == b"kept\n"
    assert after["floors.yaml"] != before["floors.yaml"]
    assert load_atlas(out).manifest["domains"] == [
        "floors",
        "labels",
        "areas",
        "devices",
        "entities",
    ]


@pytest.mark.parametrize("only", ["energy", "floors,bogus", ","])
def test_only_rejects_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, only: str) -> None:
    fake = FakeHA()
    result = run_export(monkeypatch, fake, "--out", str(tmp_path / "atlas"), "--only", only)
    assert result.exit_code == 2
    assert "--only" in result.output
    assert fake.sent == []


def test_failed_fetch_leaves_old_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    before = snapshot(out)

    data = load_fixture()
    data["floors"][0]["name"] = "Top"
    fake = FakeHA(data)
    fake.fail["config/area_registry/list"] = HAClientError("connection lost")
    result = run_export(monkeypatch, fake, "--out", str(out))
    assert result.exit_code == 1
    assert "connection lost" in result.output
    assert snapshot(out) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["atlas"]


def _assert_clean_error(result: Result, message: str) -> None:
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert f"Error: {message}" in result.output


def _leftovers(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir() if p.name.startswith(yamlio.TMP_PREFIX))


@pytest.mark.parametrize("failing_call", [1, 2, 3, 4])
def test_failed_replace_restores_old_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failing_call: int
) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    before = snapshot(out)

    calls: list[int] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        calls.append(1)
        if len(calls) == failing_call:
            raise OSError(28, "No space left on device", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(yamlio.os, "replace", replace)
    data = load_fixture()
    data["floors"][0]["name"] = "Top"
    data["labels"][0]["name"] = "Other"
    data["areas"][0]["name"] = "Other"
    result = run_export(monkeypatch, FakeHA(data), "--out", str(out))
    _assert_clean_error(result, f"cannot write {out}")
    assert "No space left on device" in result.output
    assert snapshot(out) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["atlas"]


def test_failed_rollback_is_a_clean_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0

    calls: list[int] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        calls.append(1)
        if len(calls) >= 3:  # the third rename fails, and so does every restore after it
            raise OSError(5, "Input/output error", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(yamlio.os, "replace", replace)
    data = load_fixture()
    data["floors"][0]["name"] = "Top"
    data["labels"][0]["name"] = "Other"
    data["areas"][0]["name"] = "Other"
    result = run_export(monkeypatch, FakeHA(data), "--out", str(out))
    _assert_clean_error(result, f"cannot write {out}")
    assert "Input/output error; could not restore labels.yaml, floors.yaml (their backups are left in place)" in result.output
    assert _leftovers(out), "the backups of the files that could not be restored must be kept"


def test_failed_staging_leaves_old_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    before = snapshot(out)

    def fsync(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(yamlio.os, "fsync", fsync)
    data = load_fixture()
    data["floors"][0]["name"] = "Top"
    result = run_export(monkeypatch, FakeHA(data), "--out", str(out))
    _assert_clean_error(result, f"cannot write {out}: Input/output error")
    assert snapshot(out) == before


def test_failed_write_to_new_dir_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fsync(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(yamlio.os, "fsync", fsync)
    result = run_export(monkeypatch, FakeHA(), "--out", str(tmp_path / "atlas"))
    _assert_clean_error(result, "cannot write")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_parent_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        result = run_export(monkeypatch, FakeHA(), "--out", str(parent / "atlas"))
    finally:
        parent.chmod(0o700)
    _assert_clean_error(result, "cannot write")
    assert "Permission denied" in result.output


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_in_dry_run_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    (out / "areas.yaml").chmod(0o000)
    try:
        result = run_export(monkeypatch, FakeHA(), "--out", str(out), dry_run=True)
    finally:
        (out / "areas.yaml").chmod(0o600)
    _assert_clean_error(result, f"cannot read {out / 'areas.yaml'}: Permission denied")


def test_non_utf8_file_in_dry_run_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    (out / "areas.yaml").write_bytes(b"\xff\xfe\n")
    result = run_export(monkeypatch, FakeHA(), "--out", str(out), dry_run=True)
    _assert_clean_error(result, f"cannot read {out / 'areas.yaml'}: not valid UTF-8")


def test_symlinked_out_writes_through_the_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    for _ in range(2):
        result = run_export(monkeypatch, FakeHA(), "--out", str(link))
        assert result.exit_code == 0, result.output
        assert link.is_symlink()
        assert sorted(p.name for p in target.iterdir()) == sorted(FILES)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link", "real"]


@pytest.mark.parametrize("existing", [False, True])
def test_out_dot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: bool) -> None:
    out = tmp_path / "atlas"
    if existing:
        assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    else:
        out.mkdir()
    monkeypatch.chdir(out)
    data = load_fixture()
    data["floors"][0]["name"] = "Top"
    result = run_export(monkeypatch, FakeHA(data), "--out", ".")
    assert result.exit_code == 0, result.output
    assert Path.cwd() == out
    assert sorted(p.name for p in out.iterdir()) == sorted(FILES)
    assert "name: Top" in (out / "floors.yaml").read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["atlas"]


def test_relative_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_export(monkeypatch, FakeHA(), "--out", "sub/atlas")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (tmp_path / "sub" / "atlas").iterdir()) == sorted(FILES)


def test_stale_staged_files_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    (out / f"{yamlio.TMP_PREFIX}dead-areas.yaml").write_text("x\n", encoding="utf-8")
    (out / f"{yamlio.TMP_PREFIX}dead-old-floors.yaml").write_text("x\n", encoding="utf-8")
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    assert _leftovers(out) == []
    assert sorted(p.name for p in out.iterdir()) == sorted(FILES)


def test_hidden_files_do_not_count_as_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "atlas"
    out.mkdir()
    (out / ".DS_Store").write_bytes(b"\0")
    result = run_export(monkeypatch, FakeHA(), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == sorted([".DS_Store", *FILES])


@pytest.mark.parametrize("change", ["ha_version", "hass_atlas"])
def test_only_refuses_mixed_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    before = snapshot(out)

    fake = FakeHA()
    if change == "ha_version":
        fake.ha_version = "2026.11.0"
    else:
        monkeypatch.setattr(export_cmd, "_package_version", lambda: "0.0.1")
    for dry_run in (False, True):
        result = run_export(
            monkeypatch, fake, "--out", str(out), "--only", "labels", dry_run=dry_run
        )
        _assert_clean_error(result, f"{out} holds floors, areas, devices, entities from {change}")
        assert "without --only" in result.output
    assert snapshot(out) == before

    result = run_export(monkeypatch, fake, "--out", str(out))
    assert result.exit_code == 0, result.output


def test_only_keeps_domains_listed_in_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out), "--only", "labels").exit_code == 0
    (out / "floors.yaml").write_text("[]\n", encoding="utf-8")
    assert run_export(monkeypatch, FakeHA(), "--out", str(out), "--only", "labels").exit_code == 0
    assert load_atlas(out).manifest["domains"] == ["labels"]


def test_only_refuses_broken_earlier_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    (out / "floors.yaml").unlink()
    fake = FakeHA()
    result = run_export(monkeypatch, fake, "--out", str(out), "--only", "labels")
    _assert_clean_error(result, str(out / "floors.yaml"))
    assert fake.sent == []


def test_refuses_non_atlas_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine\n", encoding="utf-8")
    fake = FakeHA()
    result = run_export(monkeypatch, fake, "--out", str(tmp_path))
    assert result.exit_code == 1
    assert "refusing" in result.output
    assert fake.sent == []
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_dry_run_new_dir_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    fake = FakeHA()
    result = run_export(monkeypatch, fake, "--out", str(out), dry_run=True)
    assert result.exit_code == 0, result.output
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    assert "+- area_id: garage_2" in result.output
    assert "Nothing written" in result.output
    assert all(cmd.endswith(READS) for cmd, _ in fake.sent)


def test_dry_run_diffs_existing_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    before = snapshot(out)

    result = run_export(monkeypatch, FakeHA(), "--out", str(out), dry_run=True)
    assert result.exit_code == 0, result.output
    assert "No changes" in result.output

    data = load_fixture()
    data["areas"][1]["floor_id"] = "ground"
    result = run_export(monkeypatch, FakeHA(data), "--out", str(out), dry_run=True)
    assert result.exit_code == 0, result.output
    assert "-  floor_id: upstairs\n+  floor_id: ground" in result.output
    assert "Would change areas.yaml" in result.output
    assert snapshot(out) == before


def test_no_timestamps_or_pictures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    text = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    for needle in ("created_at", "modified_at", "picture", "/api/image", "1760000"):
        assert needle not in text


def test_empty_domain_written_as_empty_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    data["labels"] = []
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(data), "--out", str(out)).exit_code == 0
    assert (out / "labels.yaml").read_text(encoding="utf-8") == "[]\n"
    assert load_atlas(out).docs["labels"] == []


def test_new_ha_fields_not_exported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    data["areas"][0]["future_field"] = "x"
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(data), "--out", str(out)).exit_code == 0
    assert "future_field" not in (out / "areas.yaml").read_text(encoding="utf-8")


def test_manifest_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    manifest = yaml.safe_load((out / "atlas.yaml").read_text(encoding="utf-8"))
    assert list(manifest) == ["format", "hass_atlas", "ha_version", "domains", "entities"]
    assert manifest == {
        "format": 1,
        "hass_atlas": "0.0.0",
        "ha_version": "2026.10.4",
        "domains": ["floors", "labels", "areas", "devices", "entities"],
        "entities": "all",
    }


def test_manifest_without_ha_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    fake.ha_version = None
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, fake, "--out", str(out)).exit_code == 0
    assert "ha_version" not in load_atlas(out).manifest


def test_load_atlas_matches_export(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    assert run_export(monkeypatch, FakeHA(), "--out", str(out)).exit_code == 0
    atlas = load_atlas(out)
    assert [f["floor_id"] for f in atlas.docs["floors"]] == ["upstairs", "ground", "basement"]
    assert atlas.docs["floors"][1] == {"floor_id": "ground", "name": "Ground", "level": 0}
    assert atlas.docs["areas"][3]["name"] == "Café"


def _write(path: Path, files: dict[str, str]) -> None:
    path.mkdir()
    for name, text in files.items():
        (path / name).write_text(text, encoding="utf-8")


GOOD_MANIFEST = "format: 1\nhass_atlas: 0.0.0\ndomains: [labels]\n"


@pytest.mark.parametrize(
    ("files", "where", "message"),
    [
        ({"atlas.yaml": "format: 2\ndomains: []\n"}, "atlas.yaml", "unsupported format 2"),
        ({"atlas.yaml": "format: true\ndomains: []\n"}, "atlas.yaml", "unsupported format True"),
        ({"atlas.yaml": "format: 1.0\ndomains: []\n"}, "atlas.yaml", "unsupported format 1.0"),
        ({"atlas.yaml": "format: '1'\ndomains: []\n"}, "atlas.yaml", "unsupported format '1'"),
        ({"atlas.yaml": "format: 1\ndomains: [energy]\n"}, "atlas.yaml", "unknown domains"),
        ({"atlas.yaml": "- 1\n"}, "atlas.yaml", "expected a mapping"),
        ({"atlas.yaml": GOOD_MANIFEST}, "labels.yaml", "No such file"),
        (
            {"atlas.yaml": GOOD_MANIFEST, "labels.yaml": "label_id: x\n"},
            "labels.yaml",
            "expected a list",
        ),
        (
            {"atlas.yaml": GOOD_MANIFEST, "labels.yaml": "- label_id: a\n- name: b\n"},
            "labels.yaml: item 1",
            "label_id is missing",
        ),
        (
            {"atlas.yaml": GOOD_MANIFEST, "labels.yaml": "- label_id: a\n  colour: red\n"},
            "labels.yaml: item 0",
            "unknown fields ['colour']",
        ),
        (
            {
                "atlas.yaml": GOOD_MANIFEST,
                "labels.yaml": "- label_id: a\n- label_id: b\n- label_id: a\n",
            },
            "labels.yaml: item 2",
            "duplicate label_id 'a'",
        ),
        (
            {"atlas.yaml": GOOD_MANIFEST, "labels.yaml": "- label_id: [\n"},
            "labels.yaml",
            "invalid YAML",
        ),
    ],
)
def test_load_atlas_errors(
    tmp_path: Path, files: dict[str, str], where: str, message: str
) -> None:
    path = tmp_path / "atlas"
    _write(path, files)
    with pytest.raises(AtlasFormatError) as info:
        load_atlas(path)
    assert where in str(info.value)
    assert message in str(info.value)


def test_load_atlas_rejects_non_utf8(tmp_path: Path) -> None:
    path = tmp_path / "atlas"
    _write(path, {"atlas.yaml": GOOD_MANIFEST})
    (path / "labels.yaml").write_bytes(b"- label_id: \xff\n")
    with pytest.raises(AtlasFormatError) as info:
        load_atlas(path)
    assert "labels.yaml: not valid UTF-8" in str(info.value)


def test_dump_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        yamlio.dump([{"label_id": "a", "extra": 1}], ("label_id", "name"))


def test_dump_does_not_fold_long_lines() -> None:
    text = yamlio.dump(
        [{"label_id": "a", "description": "word " * 40}], ("label_id", "description")
    )
    assert len(text.splitlines()) == 2
    assert yaml.safe_load(text) == [{"label_id": "a", "description": ("word " * 40)}]
