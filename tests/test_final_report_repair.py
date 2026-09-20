import json

import pytest
from conftest import bridge, final

FIELDS = ["status", "summary", "tests", "question", "affected_paths", "unresolved"]
FIFTY = [f"item-{index:02d}" for index in range(50)]


def fenced(body, label="json", newline="\n"):
    return "```" + label + newline + body + newline + "```"


def payload(**overrides):
    data = json.loads(final())
    data.update(overrides)
    return json.dumps(data)


def without(field, **overrides):
    data = json.loads(final())
    del data[field]
    data.update(overrides)
    return json.dumps(data)


# 1. A valid contract object inside one fence maps to the same six fields.
@pytest.mark.parametrize("label", ["json", ""], ids=["json-label", "no-label"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("outside", ["", " \n\t"], ids=["bare", "outer-whitespace"])
def test_single_json_fence_maps_to_the_same_six_fields(label, newline, outside):
    protocol = bridge("protocol")
    response = outside + fenced(final(), label=label, newline=newline) + outside

    parsed = protocol.parse_final(response)

    assert parsed.model_dump() == json.loads(final())
    assert list(parsed.model_dump()) == FIELDS


# 2. The known extra key is repaired: null dropped, strings appended in order.
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"unresolved_note": None}, []),
        ({"unresolved_note": "Follow up"}, ["Follow up"]),
        (
            {"unresolved": ["first", "second"], "unresolved_note": "third"},
            ["first", "second", "third"],
        ),
        ({"unresolved": ["first", "second"], "unresolved_note": None}, ["first", "second"]),
        ({"unresolved": FIFTY, "unresolved_note": None}, FIFTY),
    ],
    ids=["drop-null", "append-string", "append-keeps-order", "keep-existing", "fifty-with-null"],
)
def test_unresolved_note_is_repaired(overrides, expected):
    protocol = bridge("protocol")

    parsed = protocol.parse_final(final(**overrides))

    assert parsed.unresolved == expected
    assert parsed.model_dump() == json.loads(final(unresolved=expected))
    assert list(parsed.model_dump()) == FIELDS


# 3. Fence unwrap and note merge combined, with CRLF and outer whitespace.
def test_fence_and_note_repair_combined():
    protocol = bridge("protocol")
    response = (
        " \n"
        + fenced(final(unresolved=["kept"], unresolved_note="appended"), newline="\r\n")
        + "\n "
    )

    parsed = protocol.parse_final(response)

    assert parsed.model_dump() == json.loads(final(unresolved=["kept", "appended"]))


def test_fenced_null_note_is_dropped():
    protocol = bridge("protocol")
    response = fenced(final(unresolved=["kept"], unresolved_note=None))

    parsed = protocol.parse_final(response)

    assert parsed.unresolved == ["kept"]
    assert list(parsed.model_dump()) == FIELDS


# 4. Every rejection that is not exactly the two known repairs stays strict.
REJECTED = [
    pytest.param("no JSON object here", id="plain-prose"),
    pytest.param("Result follows:\n" + final() + "\nReview complete.", id="surrounding-prose"),
    pytest.param(fenced(final()) + "\n" + fenced(final()), id="multiple-fenced-blocks"),
    pytest.param(fenced(final(), label="python"), id="non-json-fence-label"),
    pytest.param("```json\n" + final() + "```", id="closing-fence-same-line"),
    pytest.param("```json " + final() + "\n```", id="body-on-opening-line"),
    pytest.param(final()[:-1], id="malformed-json"),
    pytest.param(fenced(final()[:-1]), id="malformed-fenced-json"),
    pytest.param(
        final().replace('"summary": "Done"', '"summary": "Done", "summary": "Other"'),
        id="duplicate-json-keys",
    ),
    pytest.param(
        fenced(final().replace('"summary": "Done"', '"summary": "Done", "summary": "Other"')),
        id="fenced-duplicate-json-keys",
    ),
    pytest.param(payload(extra="unexpected"), id="unknown-extra-key"),
    pytest.param(fenced(payload(extra="unexpected")), id="fenced-unknown-extra-key"),
    pytest.param(payload(extra="unexpected", unresolved_note="kept"), id="unknown-extra-with-note"),
    pytest.param(without("tests"), id="missing-required-field"),
    pytest.param(fenced(without("tests")), id="fenced-missing-required-field"),
    pytest.param(
        payload(unresolved="wrong type", unresolved_note=None), id="unresolved-not-list-null-note"
    ),
    pytest.param(
        payload(unresolved="wrong type", unresolved_note="note"),
        id="unresolved-not-list-string-note",
    ),
    pytest.param(without("unresolved"), id="unresolved-missing"),
    pytest.param(without("unresolved", unresolved_note=None), id="unresolved-missing-null-note"),
    pytest.param(
        without("unresolved", unresolved_note="note"), id="unresolved-missing-string-note"
    ),
    pytest.param(payload(unresolved_note=123), id="note-non-string"),
    pytest.param(fenced(payload(unresolved_note=[])), id="fenced-note-list"),
    pytest.param(payload(unresolved_note=""), id="note-empty"),
    pytest.param(payload(unresolved_note="x" * 501), id="note-501-chars"),
    pytest.param(
        payload(unresolved=["x"] * 50, unresolved_note="one more"), id="fifty-items-plus-note"
    ),
    pytest.param(payload(question="unexpected"), id="completed-with-question"),
    pytest.param(
        fenced(payload(status="needs_decision", question=None)), id="decision-without-question"
    ),
]


@pytest.mark.parametrize("response", REJECTED)
def test_strict_rejections_survive_normalization(response):
    protocol = bridge("protocol")

    with pytest.raises(protocol.BridgeError, match="task_contract_error"):
        protocol.parse_final(response)


@pytest.mark.parametrize("status", ["failed", "needs_decision"], ids=["failed", "needs_decision"])
def test_repair_preserves_failed_and_decision_status(status):
    protocol = bridge("protocol")
    response = fenced(final(status, unresolved_note="kept"))

    parsed = protocol.parse_final(response)

    assert parsed.status == status
    assert parsed.status != "completed"
    assert parsed.model_dump() == json.loads(final(status, unresolved=["kept"]))
    if status == "needs_decision":
        assert parsed.question == "Choose the expected behavior."
    else:
        assert parsed.question is None


@pytest.mark.parametrize("wrap", [lambda body: body, fenced], ids=["raw", "fenced"])
def test_oversize_response_is_rejected(wrap):
    protocol = bridge("protocol")
    response = wrap(final(tests=["t" * 500] * 50))

    assert len(response) > 16000
    with pytest.raises(protocol.BridgeError, match="task_contract_error"):
        protocol.parse_final(response)


@pytest.mark.parametrize("wrap", [lambda body: body, fenced], ids=["raw", "fenced"])
def test_credential_like_unresolved_note_is_rejected(wrap):
    protocol = bridge("protocol")
    canary = "sk-" + "a1b2c3d4e5f6g7h8"
    response = wrap(payload(unresolved_note=canary))

    with pytest.raises(protocol.BridgeError, match="task_contract_error") as failure:
        protocol.parse_final(response)

    assert canary not in str(failure.value)


@pytest.mark.parametrize("wrap", [lambda body: body, fenced], ids=["raw", "fenced"])
def test_escaped_credential_like_unresolved_note_is_rejected(wrap):
    protocol = bridge("protocol")
    canary = "sk-" + "a1b2c3d4e5f6g7h8"
    escaped = canary.replace("-", "\\u002d")
    response = wrap(payload(unresolved_note="placeholder").replace("placeholder", escaped))
    assert canary not in response

    with pytest.raises(protocol.BridgeError, match="task_contract_error") as failure:
        protocol.parse_final(response)

    assert canary not in str(failure.value)


# 5. The TaskManager seam: one run publishes the repaired result, unknown stays failed.
@pytest.mark.parametrize("status", ["completed", "needs_decision", "failed"])
async def test_gate_repairs_fenced_note_in_one_run(gate, repo, status):
    manager = gate.TaskManager(repo)
    manager.runtime.response = fenced(final(status, unresolved_note="kept"))
    manager.runtime.release.set()
    try:
        task = await manager.start("Repair the final report")

        result = await manager.wait(task["task_id"], 1000)

        assert result["status"] == status
        assert result["phase"] == status
        assert result["final_response"]["status"] == status
        assert result["final_response"]["unresolved"] == ["kept"]
        if status == "failed":
            assert result["error"]["class"] == "model_error"
        else:
            assert result["error"] is None
        assert len(manager.runtime.calls) == 1
        assert manager.runtime.calls[0][0] == task["session_id"]
    finally:
        await manager.shutdown()


async def test_gate_repaired_result_can_continue(gate, repo):
    manager = gate.TaskManager(repo)
    manager.runtime.response = fenced(final(unresolved_note="first follow-up"))
    manager.runtime.release.set()
    try:
        task = await manager.start("Repair the final report")
        first = await manager.wait(task["task_id"], 1000)
        assert first["status"] == "completed"
        assert first["final_response"]["unresolved"] == ["first follow-up"]
        assert len(manager.runtime.calls) == 1

        manager.runtime.response = fenced(final(unresolved=["done"], unresolved_note="next"))
        continued = await manager.continue_task(task["task_id"], "Continue once")
        assert continued["status"] == "running"
        second = await manager.wait(task["task_id"], 1000)

        assert second["status"] == "completed"
        assert second["final_response"]["unresolved"] == ["done", "next"]
        assert len(manager.runtime.calls) == 2
        assert [call[2] for call in manager.runtime.calls] == [True, False]
        assert {call[0] for call in manager.runtime.calls} == {task["session_id"]}
    finally:
        await manager.shutdown()


GATE_UNKNOWN = [
    pytest.param("Result follows:\n" + final() + "\nReview complete.", id="surrounding-prose"),
    pytest.param(fenced(final(), label="python"), id="non-json-label"),
    pytest.param(fenced(final()) + "\n" + fenced(final()), id="multiple-blocks"),
    pytest.param(
        final().replace('"summary": "Done"', '"summary": "Done", "summary": "Other"'),
        id="duplicate-keys",
    ),
]


@pytest.mark.parametrize("response", GATE_UNKNOWN)
async def test_gate_unknown_format_is_contract_error(gate, repo, response):
    manager = gate.TaskManager(repo)
    manager.runtime.response = response
    manager.runtime.release.set()
    try:
        task = await manager.start("Work")

        result = await manager.wait(task["task_id"], 1000)

        assert result["status"] == "failed"
        assert result["phase"] == "failed"
        assert result["error"]["class"] == "task_contract_error"
        assert result["final_response"] is None
    finally:
        await manager.shutdown()
