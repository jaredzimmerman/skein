"""Service rules (PLAN §C1, §C2): authorization, routing, state flow, limits."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from skein.errors import SkeinError
from skein.service import Skein
from skein.store import Store
from skein.tokens import hash_token, new_token

from conftest import T0

POINT, A, B = "claude", "muse", "helper"
DAY = 86400.0

TASK_SUMMARY_KEYS = {
    "id", "title", "assignee", "created_by", "status", "created", "claimed", "updated", "finished",
}
TASK_FULL_KEYS = TASK_SUMMARY_KEYS | {"instructions", "notes", "result"}
ASK_KEYS = {
    "id", "asker", "question", "context", "task_id", "status", "created", "answered", "answer",
    "answered_by",
}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def refused(code: str):
    with pytest.raises(SkeinError) as ei:
        yield ei
    assert ei.value.code == code, f"expected {code}, got {ei.value!r}"


@pytest.fixture
def crew(skein):
    tokens = {name: skein.add_member(name)["token"] for name in (POINT, A, B)}
    skein.set_point(POINT)
    return tokens


def new_task(skein, member=A, title="Book a table", instructions="Find a table for two on Friday."):
    return skein.assign(POINT, member, title, instructions)


def ids(items):
    return [x["id"] for x in items]


# -- tokens and errors ---------------------------------------------------------


def test_new_token_shape_and_uniqueness():
    t = new_token()
    assert t.startswith("skein_") and len(t) > 40
    assert new_token() != t


def test_hash_token_is_sha256_hex():
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_skein_error_codes_and_dict():
    e = SkeinError("not_found", "no task t_1")
    assert e.code == "not_found" and str(e) == "no task t_1"
    assert e.to_dict() == {"error": "no task t_1", "code": "not_found"}
    with pytest.raises(ValueError):
        SkeinError("teapot", "x")


# -- store ---------------------------------------------------------------------


def test_store_creates_wal_schema_version_2(store, db_path):
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"members", "meta", "tasks", "asks"} <= tables
    finally:
        conn.close()


def test_store_rejects_in_memory_db():
    with pytest.raises(ValueError):
        Store(":memory:")


def test_store_reopen_keeps_data(db_path, clock):
    Skein(Store(db_path), clock=clock).add_member(A)
    again = Skein(Store(db_path), clock=clock)
    assert [m["name"] for m in again.list_members()] == [A]


def test_store_rolls_back_failed_transaction(store):
    with pytest.raises(RuntimeError):
        with store.tx() as db:
            db.set_meta("point", "x")
            raise RuntimeError("boom")
    with store.tx(write=False) as db:
        assert db.get_meta("point") is None


def test_skein_accepts_a_path(db_path, clock):
    s = Skein(db_path, clock=clock)
    s.add_member(A)
    assert s.list_members()[0]["name"] == A


# -- members (admin) -----------------------------------------------------------


def test_add_member_returns_token_once_and_stores_only_its_hash(skein, db_path):
    out = skein.add_member(A, "Meta Muse")
    assert set(out) == {"name", "display", "token"}
    assert out["name"] == A and out["display"] == "Meta Muse"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT * FROM members WHERE name = ?", (A,)).fetchone()
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert out["token"] not in [str(v) for v in row]
    assert out["token"] not in dump
    assert hash_token(out["token"]) in dump


def test_add_member_display_defaults_to_name(skein):
    assert skein.add_member(A)["display"] == A


@pytest.mark.parametrize(
    "bad", ["", "a", "Muse", "1muse", "mu se", "mu.se", "muse\n", "m" * 33, None, 7]
)
def test_add_member_rejects_bad_names(skein, bad):
    with refused("invalid"):
        skein.add_member(bad)


@pytest.mark.parametrize("good", ["ab", "a" * 32, "a-b_c9"])
def test_add_member_accepts_edge_names(skein, good):
    assert skein.add_member(good)["name"] == good


def test_add_member_duplicate_conflicts(skein):
    skein.add_member(A)
    with refused("conflict"):
        skein.add_member(A)


def test_admin_calls_on_unknown_member_not_found(skein):
    with refused("not_found"):
        skein.rotate_token("ghost")
    with refused("not_found"):
        skein.set_disabled("ghost", True)
    with refused("not_found"):
        skein.set_point("ghost")


# -- authentication ------------------------------------------------------------


def test_authenticate(skein, crew):
    assert skein.authenticate(crew[A]) == A
    assert skein.authenticate(crew[POINT]) == POINT
    assert skein.authenticate(crew[A] + "x") is None
    assert skein.authenticate("") is None
    assert skein.authenticate(None) is None
    assert skein.authenticate("skein_") is None


def test_authenticate_disabled_member_returns_none(skein, crew):
    skein.set_disabled(A, True)
    assert skein.authenticate(crew[A]) is None
    skein.set_disabled(A, False)
    assert skein.authenticate(crew[A]) == A


def test_rotate_token_invalidates_the_old_one(skein, crew):
    out = skein.rotate_token(A)
    assert set(out) == {"name", "token"} and out["name"] == A
    assert skein.authenticate(crew[A]) is None
    assert skein.authenticate(out["token"]) == A


def test_unknown_or_disabled_caller_refused(skein, crew):
    with refused("forbidden"):
        skein.whoami("ghost")
    skein.set_disabled(B, True)
    with refused("forbidden"):
        skein.inbox(B)


# -- the point -----------------------------------------------------------------


def test_point_must_exist_and_be_enabled(skein):
    assert skein.get_point() is None
    skein.add_member(A)
    skein.set_disabled(A, True)
    with refused("conflict"):
        skein.set_point(A)
    skein.set_disabled(A, False)
    skein.set_point(A)
    assert skein.get_point() == A


def test_whoami_member_and_point(skein, crew):
    new_task(skein, A)
    new_task(skein, A)
    w = skein.whoami(A)
    assert w == {
        "member": A, "display": A, "role": "member", "point": POINT, "point_display": POINT,
    }
    p = skein.whoami(POINT)
    assert p["role"] == "point" and p["point"] == POINT
    by_name = {m["name"]: m for m in p["members"]}
    assert set(by_name) == {POINT, A, B}
    assert set(by_name[A]) == {"name", "display", "last_seen", "disabled", "open_tasks"}
    assert by_name[A]["open_tasks"] == 2 and by_name[B]["open_tasks"] == 0
    assert by_name[A]["disabled"] is False
    assert skein.members(POINT) == p["members"]


def test_whoami_without_point(skein):
    skein.add_member(A)
    w = skein.whoami(A)
    assert w["role"] == "member" and w["point"] is None and w["point_display"] is None


@pytest.mark.parametrize(
    "call",
    [
        lambda s, who, t, a: s.assign(who, B, "Title", "Do it."),
        lambda s, who, t, a: s.answer(who, a, "Yes."),
        lambda s, who, t, a: s.follow_up(who, t, "And also this."),
        lambda s, who, t, a: s.reassign(who, t, B),
        lambda s, who, t, a: s.cancel(who, t),
        lambda s, who, t, a: s.list_tasks(who),
        lambda s, who, t, a: s.list_asks(who),
        lambda s, who, t, a: s.members(who),
    ],
    ids=["assign", "answer", "follow_up", "reassign", "cancel", "list_tasks", "list_asks", "members"],
)
def test_point_only_methods_refuse_members(skein, crew, call):
    task = new_task(skein, A)
    ask = skein.ask(A, "Which restaurant?")
    for who in (A, B):
        with refused("forbidden"):
            call(skein, who, task["id"], ask["id"])
    assert skein.get_task(POINT, task["id"]) == task
    assert skein.get_ask(POINT, ask["id"])["status"] == "open"


# -- visibility ----------------------------------------------------------------


def test_members_never_see_others_tasks(skein, crew):
    task = new_task(skein, A)
    tid = task["id"]
    with refused("not_found") as missing:
        skein.get_task(B, "t_0000000000")
    calls = [
        lambda: skein.get_task(B, tid),
        lambda: skein.claim(B, tid),
        lambda: skein.note(B, tid, "hi"),
        lambda: skein.submit(B, tid, "done"),
        lambda: skein.ask(B, "About that task?", task_id=tid),
    ]
    for call in calls:
        with refused("not_found") as ei:
            call()
        # same wording as a task that doesn't exist: existence is never leaked
        assert ei.value.message.replace(tid, "X") == missing.value.message.replace(
            "t_0000000000", "X"
        )
    assert skein.inbox(B)["tasks"] == []
    assert skein.get_task(A, tid)["status"] == "queued"


def test_members_never_see_others_asks(skein, crew):
    ask = skein.ask(A, "Which day?")
    with refused("not_found"):
        skein.get_ask(B, ask["id"])
    with refused("not_found"):
        skein.get_ask(B, "a_0000000000")
    skein.answer(POINT, ask["id"], "Friday.")
    assert skein.inbox(B)["answers"] == []
    assert ids(skein.inbox(A)["answers"]) == [ask["id"]]


def test_point_reads_and_notes_any_task_but_only_assignee_claims_or_submits(skein, crew):
    task = new_task(skein, A)
    assert skein.get_task(POINT, task["id"])["id"] == task["id"]
    noted = skein.note(POINT, task["id"], "Prefer somewhere quiet.")
    assert noted["notes"][-1]["from"] == POINT
    with refused("forbidden"):
        skein.claim(POINT, task["id"])
    with refused("forbidden"):
        skein.submit(POINT, task["id"], "done")


# -- asks ----------------------------------------------------------------------


def test_point_cannot_ask(skein, crew):
    with refused("forbidden") as ei:
        skein.ask(POINT, "Can someone help?")
    assert "answer" in ei.value.message


def test_ask_without_point_conflicts(skein):
    skein.add_member(A)
    with refused("conflict"):
        skein.ask(A, "Anyone there?")


def test_ask_shape_and_task_link(skein, crew, clock):
    task = new_task(skein, A)
    ask = skein.ask(A, "Which day?", context="They said this week.", task_id=task["id"])
    assert set(ask) == ASK_KEYS
    assert ask["id"].startswith("a_") and len(ask["id"]) == 12
    assert ask["asker"] == A and ask["status"] == "open" and ask["task_id"] == task["id"]
    assert ask["created"] == iso(T0) and ask["answered"] is None and ask["answer"] is None
    assert skein.ask(A, "Plain question?")["task_id"] == ""


def test_answer_and_reanswer(skein, crew, clock):
    ask = skein.ask(A, "Which day?")
    clock.advance(60)
    out = skein.answer(POINT, ask["id"], "Friday.")
    assert out["status"] == "answered" and out["answer"] == "Friday."
    assert out["answered_by"] == POINT and out["answered"] == iso(T0 + 60)
    assert ids(skein.inbox(A)["answers"]) == [ask["id"]]
    assert skein.inbox(A)["answers"] == []
    again = skein.answer(POINT, ask["id"], "Saturday, sorry.")
    assert again["answer"] == "Saturday, sorry."
    assert [a["answer"] for a in skein.inbox(A)["answers"]] == ["Saturday, sorry."]
    with refused("not_found"):
        skein.answer(POINT, "a_0000000000", "x")


def test_get_ask_by_asker_marks_answer_seen(skein, crew):
    ask = skein.ask(A, "Which day?")
    skein.answer(POINT, ask["id"], "Friday.")
    assert skein.get_ask(POINT, ask["id"])["answer"] == "Friday."  # the point reading doesn't mark
    assert skein.get_ask(A, ask["id"])["answer"] == "Friday."
    assert skein.inbox(A)["answers"] == []


# -- assign / reassign targets -------------------------------------------------


def test_assign_refuses_point_disabled_and_unknown(skein, crew):
    with refused("conflict"):
        skein.assign(POINT, POINT, "Title", "Do it.")
    skein.set_disabled(B, True)
    with refused("conflict"):
        skein.assign(POINT, B, "Title", "Do it.")
    with refused("not_found"):
        skein.assign(POINT, "ghost", "Title", "Do it.")
    assert skein.list_tasks(POINT) == []


def test_reassign_refuses_point_disabled_and_unknown(skein, crew):
    task = new_task(skein, A)
    skein.set_disabled(B, True)
    for target, code in ((POINT, "conflict"), (B, "conflict"), ("ghost", "not_found")):
        with refused(code):
            skein.reassign(POINT, task["id"], target)
    assert skein.get_task(POINT, task["id"]) == task


def test_assign_returns_full_queued_task(skein, crew):
    task = new_task(skein, A)
    assert set(task) == TASK_FULL_KEYS
    assert task["id"].startswith("t_") and len(task["id"]) == 12
    assert task["status"] == "queued" and task["assignee"] == A and task["created_by"] == POINT
    assert task["instructions"] == "Find a table for two on Friday."
    assert task["notes"] == [] and task["result"] is None
    assert task["created"] == iso(T0) and task["claimed"] is None and task["finished"] is None


def test_reassign_moves_an_open_task(skein, crew, clock):
    task = new_task(skein, A)
    skein.claim(A, task["id"])
    clock.advance(30)
    out = skein.reassign(POINT, task["id"], B, "muse is busy")
    assert out["assignee"] == B and out["status"] == "queued" and out["claimed"] is None
    assert out["notes"][-1]["text"] == "reassigned from muse to helper: muse is busy"
    assert out["notes"][-1]["from"] == POINT
    with refused("not_found"):
        skein.get_task(A, task["id"])
    assert ids(skein.inbox(B)["tasks"]) == [task["id"]]
    back = skein.reassign(POINT, task["id"], A)
    assert back["notes"][-1]["text"] == "reassigned from helper to muse"


def test_reassign_final_task_conflicts(skein, crew):
    task = new_task(skein, A)
    skein.submit(A, task["id"], "Booked.")
    with refused("conflict"):
        skein.reassign(POINT, task["id"], B)


# -- status flow ---------------------------------------------------------------


def test_queued_claimed_done(skein, crew, clock):
    task = new_task(skein, A)
    clock.advance(10)
    claimed = skein.claim(A, task["id"])
    assert claimed["status"] == "claimed" and claimed["claimed"] == iso(T0 + 10)
    clock.advance(10)
    again = skein.claim(A, task["id"])
    assert again["claimed"] == iso(T0 + 10)  # the first claim time is kept
    clock.advance(10)
    done = skein.submit(A, task["id"], "Booked for 7pm.")
    assert done["status"] == "done" and done["result"] == "Booked for 7pm."
    assert done["finished"] == iso(T0 + 30) and done["updated"] == iso(T0 + 30)
    with refused("conflict"):
        skein.claim(A, task["id"])


def test_claimed_failed(skein, crew):
    task = new_task(skein, A)
    skein.claim(A, task["id"])
    out = skein.submit(A, task["id"], "Everything is full.", status="failed")
    assert out["status"] == "failed" and out["finished"] is not None


def test_submit_from_queued_sets_claimed(skein, crew):
    task = new_task(skein, A)
    out = skein.submit(A, task["id"], "Already done.")
    assert out["status"] == "done" and out["claimed"] == iso(T0)


def test_submit_rejects_unknown_status(skein, crew):
    task = new_task(skein, A)
    for bad in ("cancelled", "queued", "claimed", "finished", ""):
        with refused("invalid"):
            skein.submit(A, task["id"], "x", status=bad)


def test_needs_input_then_follow_up_requeues(skein, crew, clock):
    task = new_task(skein, A)
    tid = task["id"]
    skein.claim(A, tid)
    clock.advance(5)
    waiting = skein.submit(A, tid, "Which day works?", status="needs_input")
    assert waiting["status"] == "needs_input" and waiting["finished"] is None
    assert skein.inbox(A)["tasks"] == []
    assert ids(skein.inbox(POINT)["needs_input"]) == [tid]
    with refused("conflict"):
        skein.submit(A, tid, "Never mind, done.")
    with refused("conflict"):
        skein.claim(A, tid)
    clock.advance(5)
    followed = skein.follow_up(POINT, tid, "Friday.")
    assert followed["status"] == "queued" and followed["finished"] is None
    assert followed["notes"][-1] == {"t": iso(T0 + 10), "from": POINT, "text": "Friday."}
    assert ids(skein.inbox(A)["tasks"]) == [tid]
    assert skein.claim(A, tid)["status"] == "claimed"
    assert skein.submit(A, tid, "Booked Friday.")["status"] == "done"


def test_follow_up_on_claimed_task_requeues(skein, crew):
    task = new_task(skein, A)
    skein.claim(A, task["id"])
    assert skein.follow_up(POINT, task["id"], "Make it for three.")["status"] == "queued"


def test_seen_by_point_set_on_done_failed_needs_input(skein, crew):
    tasks = [new_task(skein, A, title=f"t{i}") for i in range(3)]
    for t, st in zip(tasks, ("done", "failed", "needs_input")):
        skein.submit(A, t["id"], "r", status=st)
    inbox = skein.inbox(POINT)
    assert ids(inbox["finished"]) == [tasks[0]["id"], tasks[1]["id"]]
    assert ids(inbox["needs_input"]) == [tasks[2]["id"]]


# -- amend window --------------------------------------------------------------


def test_amend_inside_seven_days(skein, crew, clock):
    task = new_task(skein, A)
    tid = task["id"]
    skein.submit(A, tid, "v1")
    clock.advance(7 * DAY - 60)
    assert skein.note(A, tid, "fyi: they confirmed")["notes"][-1]["text"] == "fyi: they confirmed"
    amended = skein.submit(A, tid, "v2")
    assert amended["result"] == "v2" and amended["status"] == "done"
    server = [n for n in amended["notes"] if n["from"] == "server"]
    assert server[-1]["text"] == "result amended; previous: v1"
    assert amended["finished"] == iso(T0 + 7 * DAY - 60)
    followed = skein.follow_up(POINT, tid, "One more thing.")
    assert followed["status"] == "queued" and followed["finished"] is None


def test_amend_note_keeps_first_1500_chars(skein, crew):
    task = new_task(skein, A)
    first = "a" * 1500 + "b" * 500
    skein.submit(A, task["id"], first)
    out = skein.submit(A, task["id"], "short")
    assert out["notes"][-1]["text"] == "result amended; previous: " + "a" * 1500


def test_no_amend_after_seven_days(skein, crew, clock):
    task = new_task(skein, A)
    tid = task["id"]
    skein.submit(A, tid, "v1", status="failed")
    clock.advance(7 * DAY + 1)
    with refused("conflict"):
        skein.note(A, tid, "late note")
    with refused("conflict"):
        skein.note(POINT, tid, "late note")
    with refused("conflict"):
        skein.submit(A, tid, "v2")
    with refused("conflict"):
        skein.follow_up(POINT, tid, "too late")
    assert skein.get_task(POINT, tid)["result"] == "v1"


# -- cancel --------------------------------------------------------------------


def test_cancel_open_task_then_nothing_is_accepted(skein, crew, clock):
    task = new_task(skein, A)
    tid = task["id"]
    skein.claim(A, tid)
    clock.advance(5)
    out = skein.cancel(POINT, tid)
    assert out["status"] == "cancelled" and out["finished"] == iso(T0 + 5)
    for call in (
        lambda: skein.claim(A, tid),
        lambda: skein.note(A, tid, "x"),
        lambda: skein.submit(A, tid, "x"),
        lambda: skein.note(POINT, tid, "x"),
        lambda: skein.follow_up(POINT, tid, "x"),
        lambda: skein.reassign(POINT, tid, B),
    ):
        with refused("conflict"):
            call()
    assert skein.cancel(POINT, tid) == {"id": tid, "status": "cancelled", "note": "already final"}
    assert skein.inbox(A)["tasks"] == []


def test_cancel_final_task_changes_nothing(skein, crew):
    task = new_task(skein, A)
    done = skein.submit(A, task["id"], "Booked.")
    out = skein.cancel(POINT, task["id"])
    assert out == {"id": task["id"], "status": "done", "note": "already final"}
    assert skein.get_task(POINT, task["id"]) == done


def test_cancel_unknown_task_not_found(skein, crew):
    with refused("not_found"):
        skein.cancel(POINT, "t_0000000000")


# -- inbox ---------------------------------------------------------------------


def test_member_inbox(skein, crew, clock):
    t1 = new_task(skein, A, title="one")
    clock.advance(1)
    t2 = new_task(skein, A, title="two")
    new_task(skein, B, title="not yours")
    skein.claim(A, t2["id"])
    clock.advance(1)
    inbox = skein.inbox(A)
    assert set(inbox) == {"role", "point", "tasks", "answers", "checked_in_at"}
    assert inbox["role"] == "member" and inbox["point"] == POINT
    assert ids(inbox["tasks"]) == [t1["id"], t2["id"]]  # oldest first
    assert set(inbox["tasks"][0]) == TASK_FULL_KEYS
    assert inbox["answers"] == [] and inbox["checked_in_at"] == iso(T0 + 2)
    last_seen = {m["name"]: m["last_seen"] for m in skein.members(POINT)}
    assert last_seen[A] == iso(T0 + 2) and last_seen[B] is None


def test_member_inbox_shows_each_answer_once(skein, crew):
    a1 = skein.ask(A, "First?")
    a2 = skein.ask(A, "Second?")
    skein.answer(POINT, a1["id"], "One.")
    skein.answer(POINT, a2["id"], "Two.")
    assert [a["answer"] for a in skein.inbox(A)["answers"]] == ["One.", "Two."]
    assert skein.inbox(A)["answers"] == []


def test_point_inbox(skein, crew, clock):
    a1 = skein.ask(A, "First?")
    clock.advance(1)
    a2 = skein.ask(B, "Second?")
    waiting = new_task(skein, A, title="waiting")
    done = new_task(skein, A, title="done")
    failed = new_task(skein, B, title="failed")
    skein.submit(A, waiting["id"], "Which day?", status="needs_input")
    skein.submit(A, done["id"], "Booked.")
    skein.submit(B, failed["id"], "Closed.", status="failed")
    first = skein.inbox(POINT)
    assert set(first) == {"role", "asks", "needs_input", "finished", "checked_in_at"}
    assert first["role"] == "point"
    assert ids(first["asks"]) == [a1["id"], a2["id"]]
    assert ids(first["needs_input"]) == [waiting["id"]]
    assert ids(first["finished"]) == [done["id"], failed["id"]]
    assert first["finished"][0]["result"] == "Booked."
    second = skein.inbox(POINT)
    assert second["finished"] == []  # marked seen
    assert ids(second["needs_input"]) == [waiting["id"]]  # still waiting
    assert ids(second["asks"]) == [a1["id"], a2["id"]]  # open until answered
    skein.answer(POINT, a1["id"], "Yes.")
    assert ids(skein.inbox(POINT)["asks"]) == [a2["id"]]
    skein.submit(A, done["id"], "Booked, moved to 8pm.")  # amended: surfaces again
    assert ids(skein.inbox(POINT)["finished"]) == [done["id"]]


def test_inbox_limit_clamps(skein, crew):
    for i in range(30):
        skein.assign(POINT, A, f"t{i}", "x")
    assert len(skein.inbox(A, limit=100)["tasks"]) == 25
    assert len(skein.inbox(A, limit=0)["tasks"]) == 1
    assert len(skein.inbox(A, limit="5")["tasks"]) == 5
    assert len(skein.inbox(A)["tasks"]) == 10
    with refused("invalid"):
        skein.inbox(A, limit="lots")


def test_point_inbox_marks_only_what_it_returned(skein, crew):
    tasks = [new_task(skein, A, title=f"t{i}") for i in range(3)]
    for t in tasks:
        skein.submit(A, t["id"], "ok")
    assert ids(skein.inbox(POINT, limit=2)["finished"]) == ids(tasks[:2])
    assert ids(skein.inbox(POINT, limit=2)["finished"]) == ids(tasks[2:])


# -- moving the point ----------------------------------------------------------


def test_moving_the_point(skein, crew):
    mine = new_task(skein, A)  # assigned to muse, who is about to become the point
    open_ask = skein.ask(B, "Which day?")
    skein.set_point(A)
    assert skein.whoami(POINT)["role"] == "member"
    assert skein.whoami(A)["role"] == "point"
    assert skein.whoami(B)["point"] == A
    assert ids(skein.inbox(A)["asks"]) == [open_ask["id"]]
    with refused("forbidden"):
        skein.assign(POINT, B, "Title", "Do it.")
    with refused("forbidden"):
        skein.ask(A, "Can I still ask?")
    old_point_ask = skein.ask(POINT, "Now I'm a member: what next?")
    assert ids(skein.inbox(A)["asks"]) == [open_ask["id"], old_point_ask["id"]]
    kept = skein.get_task(A, mine["id"])
    assert kept["assignee"] == A and kept["status"] == "queued"
    with refused("not_found"):
        skein.get_task(POINT, mine["id"])
    assert skein.reassign(A, mine["id"], B)["assignee"] == B


# -- limits --------------------------------------------------------------------


def test_title_truncated_to_200(skein, crew):
    task = skein.assign(POINT, A, "x" * 500, "Do it.")
    assert len(task["title"]) == 200


def test_instructions_over_8000_rejected(skein, crew):
    with refused("invalid"):
        skein.assign(POINT, A, "Title", "x" * 8001)
    assert len(skein.assign(POINT, A, "Title", "x" * 8000)["instructions"]) == 8000


def test_result_truncated_to_50000(skein, crew):
    task = new_task(skein, A)
    out = skein.submit(A, task["id"], "r" * 60000)
    assert len(out["result"]) == 50000 and out["result"].startswith("r" * 100)


def test_note_truncated_to_2000(skein, crew):
    task = new_task(skein, A)
    assert len(skein.note(A, task["id"], "n" * 3000)["notes"][-1]["text"]) == 2000
    assert len(skein.follow_up(POINT, task["id"], "f" * 3000)["notes"][-1]["text"]) == 2000
    long = skein.reassign(POINT, task["id"], B, "z" * 3000)
    assert len(long["notes"][-1]["text"]) == 2000


def test_question_over_4000_rejected(skein, crew):
    with refused("invalid"):
        skein.ask(A, "q" * 4001)
    assert len(skein.ask(A, "q" * 4000)["question"]) == 4000


def test_context_truncated_to_8000(skein, crew):
    assert len(skein.ask(A, "Why?", context="c" * 9000)["context"]) == 8000


def test_answer_truncated_to_50000(skein, crew):
    ask = skein.ask(A, "Why?")
    assert len(skein.answer(POINT, ask["id"], "a" * 60000)["answer"]) == 50000


def test_empty_text_is_invalid(skein, crew):
    task = new_task(skein, A)
    ask = skein.ask(A, "Why?")
    for call in (
        lambda: skein.assign(POINT, A, "", "Do it."),
        lambda: skein.assign(POINT, A, "   ", "Do it."),
        lambda: skein.assign(POINT, A, "Title", ""),
        lambda: skein.assign(POINT, A, "Title", None),
        lambda: skein.submit(A, task["id"], ""),
        lambda: skein.note(A, task["id"], "  \n"),
        lambda: skein.follow_up(POINT, task["id"], ""),
        lambda: skein.ask(A, ""),
        lambda: skein.answer(POINT, ask["id"], ""),
        lambda: skein.assign(POINT, A, 12, "Do it."),
    ):
        with refused("invalid"):
            call()


def test_notes_keep_the_last_50(skein, crew):
    task = new_task(skein, A)
    for i in range(55):
        out = skein.note(A, task["id"], f"note {i}")
    assert len(out["notes"]) == 50
    assert out["notes"][0]["text"] == "note 5" and out["notes"][-1]["text"] == "note 54"


def test_list_limits_clamp(skein, crew):
    for i in range(3):
        new_task(skein, A, title=f"t{i}")
        skein.ask(A, f"q{i}?")
    assert len(skein.list_tasks(POINT, limit=0)) == 1
    assert len(skein.list_tasks(POINT, limit=1000)) == 3
    assert len(skein.list_asks(POINT, limit=-5)) == 1
    with refused("invalid"):
        skein.list_tasks(POINT, limit="many")


def test_list_limit_caps_at_100(skein, crew):
    with skein.store.tx() as db:  # seed directly: 120 tasks through assign is slow on a busy box
        for i in range(120):
            db.insert_task({
                "id": f"t_{i:010x}", "title": f"t{i}", "body": "x", "assignee": A,
                "created_by": POINT, "status": "queued", "created": T0 + i, "updated": T0 + i,
            })
    assert len(skein.list_tasks(POINT, limit=1000)) == 100


# -- listing -------------------------------------------------------------------


def test_list_tasks_newest_first_with_filters(skein, crew, clock):
    t1 = new_task(skein, A, title="one")
    clock.advance(1)
    t2 = new_task(skein, B, title="two")
    clock.advance(1)
    t3 = new_task(skein, A, title="three")
    skein.submit(A, t3["id"], "done")
    listed = skein.list_tasks(POINT)
    assert ids(listed) == [t3["id"], t2["id"], t1["id"]]
    assert set(listed[0]) == TASK_SUMMARY_KEYS
    assert ids(skein.list_tasks(POINT, member=A)) == [t3["id"], t1["id"]]
    assert ids(skein.list_tasks(POINT, status="done")) == [t3["id"]]
    assert ids(skein.list_tasks(POINT, status="open")) == [t2["id"], t1["id"]]
    assert ids(skein.list_tasks(POINT, status="queued", member=B)) == [t2["id"]]
    with refused("invalid"):
        skein.list_tasks(POINT, status="sideways")


def test_list_asks_orders(skein, crew, clock):
    a1 = skein.ask(A, "One?")
    clock.advance(1)
    a2 = skein.ask(B, "Two?")
    clock.advance(1)
    a3 = skein.ask(A, "Three?")
    skein.answer(POINT, a2["id"], "Yes.")
    assert ids(skein.list_asks(POINT)) == [a1["id"], a3["id"]]  # open, oldest first
    assert ids(skein.list_asks(POINT, status="answered")) == [a2["id"]]
    assert ids(skein.list_asks(POINT, status="all")) == [a3["id"], a2["id"], a1["id"]]
    assert set(skein.list_asks(POINT)[0]) == ASK_KEYS
    with refused("invalid"):
        skein.list_asks(POINT, status="closed")


# -- admin status --------------------------------------------------------------


def test_status(skein, crew):
    t = new_task(skein, A)
    new_task(skein, B)
    skein.submit(A, t["id"], "done")
    skein.ask(A, "Why?")
    st = skein.status()
    assert st["point"] == POINT
    assert st["open_tasks"] == 1 and st["open_asks"] == 1
    assert {m["name"] for m in st["members"]} == {POINT, A, B}
    assert st["members"] == skein.list_members()


def test_disabled_member_listed_as_disabled(skein, crew):
    skein.set_disabled(B, True)
    flags = {m["name"]: m["disabled"] for m in skein.list_members()}
    assert flags == {POINT: False, A: False, B: True}


# -- T10 rule fixes ------------------------------------------------------------


@pytest.mark.parametrize("name", ["server", "point", "all", "skein"])
def test_add_member_rejects_reserved_names(skein, name):
    with refused("invalid"):
        skein.add_member(name)


def test_set_disabled_refuses_current_point(skein, crew):
    with refused("conflict") as ei:
        skein.set_disabled(POINT, True)
    assert "move the point first (skein point set NAME)" in str(ei.value)
    assert skein.set_disabled(POINT, False)["disabled"] is False
    assert skein.set_disabled(A, True)["disabled"] is True


def test_submit_replacing_open_result_notes_it_once(skein, crew):
    tid = new_task(skein, A)["id"]
    skein.submit(A, tid, "Which day?", status="needs_input")
    skein.follow_up(POINT, tid, "Friday.")
    out = skein.submit(A, tid, "Booked for Friday.")
    replaced = [n for n in out["notes"] if n["from"] == "server"]
    assert len(replaced) == 1
    assert replaced[0]["text"] == "result replaced; previous: Which day?"


def test_submit_replacing_final_result_adds_exactly_one_server_note(skein, crew):
    tid = new_task(skein, A)["id"]
    skein.submit(A, tid, "v1")
    out = skein.submit(A, tid, "v2")
    assert [n["text"] for n in out["notes"] if n["from"] == "server"] == [
        "result amended; previous: v1"
    ]
