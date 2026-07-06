"""PLAN-side gate that rejects hallucinated search blocks before EXECUTE and
feeds the real file lines back so the planner can copy a valid block."""

import json

from src import new_agent
from src.nodes import plan as plan_node
from src.state import AgentState, FileInfo, PatchEdit, Phase

REAL = (
    "\n".join(f"import mod{i}" for i in range(30))
    + "\ndef compute(value):\n    return value * 2\n"
)


def _state(**kw):
    return AgentState(
        issue_url="https://github.com/acme/widget/issues/7",
        issue_title="compute is wrong",
        issue_body="compute returns the wrong number",
        current_phase=Phase.PLAN,
        relevant_files=[
            FileInfo(path="app/mod.py", content=REAL, relevance_score=0.9, reason="r")
        ],
        **kw,
    )


def _plan_response(file_path, search, replace):
    return json.dumps(
        {
            "plan": "fix it",
            "patch": "",
            "patch_edits": [{"file": file_path, "search": search, "replace": replace}],
            "files": [file_path],
            "test_command": "pytest",
            "decision_frame": {
                "stage": "plan",
                "summary": "fix",
                "recommended_action": "execute",
                "risk": "low",
                "confidence": 0.7,
            },
        }
    )


# ---- pure predicates --------------------------------------------------------

def test_unlocatable_edits_flags_hallucinated_search():
    state = _state()
    state.patch_edits = [
        PatchEdit(file_path="app/mod.py", search="def compute(val):\n    return val*2",
                  replace="x")
    ]
    missing = plan_node._unlocatable_edits(state)
    assert len(missing) == 1


def test_unlocatable_edits_empty_when_search_present():
    state = _state()
    state.patch_edits = [
        PatchEdit(file_path="app/mod.py", search="    return value * 2", replace="x")
    ]
    assert plan_node._unlocatable_edits(state) == []


def test_unlocatable_edits_skips_unknown_file():
    # File not in relevant_files → cannot validate → skipped (EXECUTE backstops).
    state = _state()
    state.patch_edits = [
        PatchEdit(file_path="other/unknown.py", search="whatever", replace="x")
    ]
    assert plan_node._unlocatable_edits(state) == []


def test_build_search_correction_surfaces_real_lines():
    state = _state()
    missing = [
        PatchEdit(file_path="app/mod.py", search="def compute(val):\n    return val*2",
                  replace="x")
    ]
    text = plan_node._build_search_correction(state, missing)
    assert "VERBATIM" in text
    assert "def compute(value):" in text  # the ACTUAL line, not the hallucination
    assert "return value * 2" in text


# C1: feed back the WHOLE real node, not just a few nearby lines ---------------

_CLASS_SRC = (
    "\n".join(f"import mod{i}" for i in range(30))
    + "\nclass Widget:\n"
    "    def __init__(self, *, name):\n"
    "        self.name = name\n"
    "        self.count = 0\n"
    "        self.ready = False\n"
)


def _class_state(**kw):
    return AgentState(
        issue_url="https://github.com/acme/widget/issues/8",
        issue_title="init is wrong",
        issue_body="Widget init",
        current_phase=Phase.PLAN,
        relevant_files=[
            FileInfo(path="app/mod.py", content=_CLASS_SRC, relevance_score=0.9,
                     reason="r")
        ],
        **kw,
    )


def test_build_search_correction_feeds_full_node_source():
    # The model located Widget.__init__ but mis-remembered its signature line
    # (wrote a default + dropped `*,`). The body line it DID get right anchors
    # the node, and the whole real __init__ is handed back.
    state = _class_state()
    missing = [
        PatchEdit(
            file_path="app/mod.py",
            search="    def __init__(self, name='x'):\n        self.name = name",
            replace="x",
        )
    ]
    text = plan_node._build_search_correction(state, missing)
    assert "full source of `Widget.__init__`" in text
    assert "def __init__(self, *, name):" in text  # the REAL signature
    assert "self.ready = False" in text  # a body line far from the anchor


def test_build_search_correction_falls_back_when_no_node():
    # Nothing in the search appears verbatim → no node resolves → closest_region.
    state = _state()
    missing = [
        PatchEdit(file_path="app/mod.py",
                  search="totally unrelated text that is nowhere", replace="x")
    ]
    text = plan_node._build_search_correction(state, missing)
    assert "full source of" not in text
    assert "nearest your intent" in text


# ---- routing ----------------------------------------------------------------

async def test_hallucinated_search_routes_to_plan_with_correction(monkeypatch):
    async def fake_llm_call(system, user):
        return _plan_response("app/mod.py", "def compute(val):\n    return val*2", "y")

    monkeypatch.setattr(plan_node, "llm_call", fake_llm_call)
    state = _state(retry_count=0, max_retries=3)

    nxt = await plan_node.plan_fix(state)

    assert nxt.current_phase == Phase.PLAN
    assert nxt.patch_edits == []
    assert nxt.hallucinated_search_block_count == 1
    assert "VERBATIM" in nxt.search_correction_context
    assert any(
        w.get("warning") == "hallucinated_search_block" for w in nxt.decision_warnings
    )
    # The router keys off frame.recommended_action, not current_phase — the gate
    # must reroute the frame or the empty patch leaks to EXECUTE (the "No valid
    # patches in input" bug). Verify the frame agrees with current_phase.
    assert nxt.decision_frame.recommended_action == "plan"
    from src import new_agent
    assert new_agent.route_from_state(nxt) == "plan_fix"


async def test_hallucinated_search_fails_fast_when_budget_exhausted(monkeypatch):
    async def fake_llm_call(system, user):
        return _plan_response("app/mod.py", "def compute(val):\n    return val*2", "y")

    monkeypatch.setattr(plan_node, "llm_call", fake_llm_call)
    state = _state(retry_count=0, max_retries=3,
                   hallucinated_search_block_count=plan_node.MAX_SEARCH_CORRECTIONS)

    nxt = await plan_node.plan_fix(state)

    assert nxt.current_phase == Phase.FAILURE
    assert "do not exist" in nxt.failure_reason


async def test_locatable_search_executes_and_clears_correction(monkeypatch):
    async def fake_llm_call(system, user):
        return _plan_response("app/mod.py", "    return value * 2", "    return value * 3")

    monkeypatch.setattr(plan_node, "llm_call", fake_llm_call)
    state = _state(retry_count=0, max_retries=3,
                   search_correction_context="stale correction text")

    nxt = await plan_node.plan_fix(state)

    assert nxt.current_phase == Phase.EXECUTE
    assert nxt.search_correction_context == ""


async def test_correction_context_injected_into_prompt(monkeypatch):
    captured = {}

    async def fake_llm_call(system, user):
        captured["user"] = user
        return _plan_response("app/mod.py", "    return value * 2", "    return value * 3")

    monkeypatch.setattr(plan_node, "llm_call", fake_llm_call)
    state = _state(retry_count=0, max_retries=3,
                   search_correction_context="COPY-THIS-VERBATIM-MARKER")

    await plan_node.plan_fix(state)

    assert "COPY-THIS-VERBATIM-MARKER" in captured["user"]
