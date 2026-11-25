"""The fake wtctl: throwaway ``create``, ``remove`` and ``list --throwaways``."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from review_run.gateway import CallTimeout
from tests.fakes import FakeBin

REPO = "github.com/acme/widgets"
SHA = "a1" * 20
CREATE = ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--source", "auto",
          "--label", "owner=review-run", "--label", "run=rr-abcd2345", "--actor", "auto:review-run",
          "--json"]  # fmt: skip


def run(fb: FakeBin, *argv: str) -> tuple[int, Any, str]:
    result = fb.run("wtctl", *argv)
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = None
    return result.returncode, doc, result.stderr


def create(fb: FakeBin) -> dict[str, Any]:
    code, doc, _ = run(fb, *CREATE)
    assert code == 0, doc
    return doc["record"]


def test_create_a_throwaway_with_the_exact_review_run_argv(fakebin: FakeBin) -> None:
    rec = create(fakebin)
    assert rec["object_type"] == "throwaway" and rec["state"] == "active" and rec["ref"] == SHA
    assert rec["labels"] == ["owner=review-run", "run=rr-abcd2345"]
    assert os.path.isdir(rec["path"])
    assert fakebin.calls("wtctl")[0].argv == CREATE


def test_create_ids_are_distinct(fakebin: FakeBin) -> None:
    assert create(fakebin)["id"] != create(fakebin)["id"]


def test_list_throwaways_filters_and_orders(fakebin: FakeBin) -> None:
    a, b = create(fakebin), create(fakebin)
    code, doc, _ = run(fakebin, "list", "--throwaways", "--label", "run=rr-abcd2345", "--json")
    assert code == 0 and doc["foreign"] == []
    assert {r["id"] for r in doc["records"]} == {a["id"], b["id"]}
    _, doc, _ = run(fakebin, "list", "--throwaways", "--label", "run=other", "--json")
    assert doc["records"] == []
    _, doc, _ = run(fakebin, "list", "--worktrees", "--json")
    assert doc["records"] == []
    _, doc, _ = run(fakebin, "list", "--repo", "github.com/acme/other", "--json")
    assert doc["records"] == []


def test_remove_then_the_record_is_a_tombstone(fakebin: FakeBin) -> None:
    rec = create(fakebin)
    code, doc, _ = run(fakebin, "remove", rec["id"], "--actor", "auto:review-run", "--json")
    assert code == 0 and doc["record"]["state"] == "removed" and doc["overridden"] == []
    assert not os.path.exists(rec["path"])
    _, listed, _ = run(fakebin, "list", "--throwaways", "--json")
    assert listed["records"] == []
    _, listed, _ = run(fakebin, "list", "--throwaways", "--state", "removed", "--json")
    assert [r["id"] for r in listed["records"]] == [rec["id"]]
    code, doc, _ = run(fakebin, "remove", rec["id"], "--json")
    assert code == 10 and doc["error"]["code"] == "not_found"


def test_remove_unknown_and_dry_run(fakebin: FakeBin) -> None:
    assert run(fakebin, "remove", "wt-aaaaaaaa", "--json")[0] == 10
    rec = create(fakebin)
    code, doc, _ = run(fakebin, "remove", rec["id"], "--dry-run", "--json")
    assert code == 0 and doc["dry_run"] is True and doc["would_remove"] is True
    assert os.path.isdir(rec["path"])


@pytest.mark.parametrize(
    ("name", "code", "error"),
    [
        ("ref_not_found", 12, "ref_not_found"),
        ("fetch_failed", 12, "fetch_failed"),
        ("auth_required", 12, "auth_required"),
        ("create_vetoed", 11, "vetoed"),
        ("create_escalated", 13, "escalated"),
    ],
)
def test_create_failure_scenarios(fakebin: FakeBin, name: str, code: int, error: str) -> None:
    fakebin.use("wtctl", name)
    got, doc, _ = run(fakebin, *CREATE)
    assert (got, doc["error"]["code"]) == (code, error)
    assert fakebin.state().get("wtctl", {"records": {}})["records"] == {}


def test_remove_in_use_then_ok(fakebin: FakeBin) -> None:
    rec = create(fakebin)
    fakebin.use("wtctl", "remove_in_use")
    code, doc, _ = run(fakebin, "remove", rec["id"], "--json")
    assert code == 20 and doc["error"]["reason"] == "in_use"
    assert run(fakebin, "remove", rec["id"], "--json")[0] == 0


def test_remove_busy_and_partial(fakebin: FakeBin) -> None:
    rec = create(fakebin)
    fakebin.use("wtctl", "remove_busy")
    assert run(fakebin, "remove", rec["id"], "--json")[0] == 12
    fakebin.use("wtctl", "remove_partial")
    code, doc, _ = run(fakebin, "remove", rec["id"], "--json")
    assert code == 1 and doc["error"]["code"] == "partial"


def test_remove_in_use_always_stays_refused(fakebin: FakeBin) -> None:
    rec = create(fakebin)
    fakebin.use("wtctl", "remove_in_use_always")
    assert run(fakebin, "remove", rec["id"], "--json")[0] == 20
    assert run(fakebin, "remove", rec["id"], "--json")[0] == 20


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "--throwaway", "--repo", REPO, "--json"],  # no --ref
        ["create", "--throwaway", "--ref", SHA, "--json"],  # no --repo
        ["create", "--throwaway", "--repo", REPO, "--ref", "abc123", "--json"],  # short sha
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--source", "cloud", "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--depth", "-1", "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--branch", "b", "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--sparse", "d", "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--label", "Bad=1", "--json"],
        ["create", "--throwaway", "--repo", "not a repo", "--ref", SHA, "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--override-escalation", "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--actor", "me", "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--thrwaway", "--json"],
        ["remove", "--json"],
        ["remove", "a", "b", "--json"],
        ["remove", "wt-aaaaaaaa", "--force", "--json"],  # human-only
        ["remove", "wt-aaaaaaaa", "--force", "--actor", "auto:x", "--json"],
        ["remove", "wt-aaaaaaaa", "--merged-head", "abc", "--json"],
        ["list", "--throwaways", "--worktrees", "--json"],
        ["list", "--older-than", "0", "--json"],
        ["list", "--older-than", "1.5d", "--json"],  # DURATION is ^[1-9][0-9]*[smhdw]$
        ["list", "--older-than", "10ms", "--json"],
        ["list", "--older-than", "10", "--json"],
        ["list", "--state", "bogus", "--json"],
        ["list", "extra", "--json"],
        ["frob", "--json"],
        ["create", "--throwaway", "--repo", REPO, "--ref", SHA, "--verbose", "--quiet", "--json"],
    ],
)
def test_the_grammar_rejects_what_the_real_tool_rejects(fakebin: FakeBin, argv: list[str]) -> None:
    code, doc, err = run(fakebin, *argv)
    assert code == 2, argv
    assert doc["error"]["code"] == "usage" and err.startswith("wtctl: error:")


def test_the_removal_of_a_tombstone_carries_its_state_and_time(fakebin: FakeBin) -> None:
    rec = create(fakebin)
    run(fakebin, "remove", rec["id"], "--json")
    code, doc, _ = run(fakebin, "remove", rec["id"], "--json")
    assert code == 10 and doc["error"]["details"]["state"] == "removed"
    assert doc["error"]["details"]["removed_at"] is not None
    # only a full id finds a tombstone, never its path
    code, doc, _ = run(fakebin, "remove", rec["path"], "--json")
    assert code == 10 and "details" not in doc["error"]


@pytest.mark.parametrize(
    "flag",
    [["--force", "--actor", "human:me"], ["--merged-head", "a" * 40], ["--keep-branch"],
     ["--holder", "me"]],
)  # fmt: skip
def test_remove_of_a_throwaway_refuses_the_worktree_flags(
    fakebin: FakeBin, flag: list[str]
) -> None:
    rec = create(fakebin)
    code, doc, _ = run(fakebin, "remove", rec["id"], *flag, "--json")
    assert code == 2 and doc["error"]["code"] == "usage"
    assert os.path.isdir(rec["path"])  # nothing was removed


def test_depth_zero_is_the_full_history(fakebin: FakeBin) -> None:
    code, doc, _ = run(fakebin, *CREATE, "--depth", "0")
    assert code == 0 and doc["record"]["depth"] == 0


def test_a_repeated_scalar_flag_keeps_the_last_value(fakebin: FakeBin) -> None:
    code, doc, _ = run(fakebin, *CREATE, "--repo", "github.com/acme/other", "--depth", "3",
                       "--depth", "5")  # fmt: skip
    assert code == 0 and doc["record"]["repo"] == "github.com/acme/other"
    assert doc["record"]["depth"] == 5


def test_list_older_than_filters_on_the_created_time(fakebin: FakeBin) -> None:
    old, new = create(fakebin), create(fakebin)
    fakebin.seed_state({"wtctl": {"records": {old["id"]: {"created": "2020-01-01T00:00:00Z"}}}})
    _, doc, _ = run(fakebin, "list", "--throwaways", "--older-than", "7d", "--json")
    assert [r["id"] for r in doc["records"]] == [old["id"]]  # validated against the pinned schema
    assert new["id"] not in {r["id"] for r in doc["records"]}


def test_remove_by_path_prefers_the_live_record_over_a_tombstone(fakebin: FakeBin) -> None:
    gone, live = create(fakebin), create(fakebin)
    run(fakebin, "remove", gone["id"], "--json")
    fakebin.seed_state({"wtctl": {"records": {gone["id"]: {"path": live["path"]}}}})
    code, doc, _ = run(fakebin, "remove", live["path"], "--json")
    assert code == 0 and doc["record"]["id"] == live["id"]


def test_a_human_actor_allows_override_escalation_but_a_throwaway_takes_no_force(
    fakebin: FakeBin,
) -> None:
    rec = create(fakebin)
    assert run(fakebin, "remove", rec["id"], "--force", "--actor", "human:me", "--json")[0] == 2
    human = ("--override-escalation", "--actor", "human:me", "--json")
    assert run(fakebin, "remove", rec["id"], *human)[0] == 0


def test_the_worktree_form_is_not_emulated(fakebin: FakeBin) -> None:
    assert run(fakebin, "create", "--repo", REPO, "--branch", "b", "--json")[0] == 99
    assert run(fakebin, "status", "x", "--json")[0] == 99


def test_dry_run_create_changes_nothing(fakebin: FakeBin) -> None:
    code, doc, _ = run(fakebin, *CREATE, "--dry-run")
    assert code == 0 and doc["dry_run"] is True
    assert fakebin.state().get("wtctl", {"records": {}})["records"] == {}


def test_version(fakebin: FakeBin) -> None:
    assert fakebin.run("wtctl", "--version").stdout == "wtctl 1.2.0\n"


def test_hang_is_ended_by_the_gateway_timeout(fakebin: FakeBin) -> None:
    fakebin.use("wtctl", "hang")
    with pytest.raises(CallTimeout):
        fakebin.run("wtctl", *CREATE, timeout=0.6)


def test_ok_scenario_is_the_default(fakebin: FakeBin) -> None:
    fakebin.use("wtctl", "ok")
    assert create(fakebin)["state"] == "active"
