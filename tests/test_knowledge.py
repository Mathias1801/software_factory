from factory.artifacts import ArtifactStore
from factory.knowledge import Knowledge


def test_lessons_are_shared_across_projects(tmp_path):
    a = Knowledge(ArtifactStore(tmp_path / "project_a"))
    a.record_lesson(problem="tests/test_api.py::test_login|AssertionError token expiry", fix="Use timezone-aware datetimes",
                    tags=["security"], context="login endpoint")
    b = Knowledge(ArtifactStore(tmp_path / "project_b"))
    hint = b.lessons_for("Implement login endpoint with token expiry", ["security"])
    assert "timezone-aware" in hint
    assert b.lessons_for("Render a pie chart", []) == ""


def test_avg_task_tokens(tmp_path):
    store = ArtifactStore(tmp_path / "p")
    store.append_text("telemetry.jsonl", '{"event": "task_result", "outcome": "passed", "tokens_spent": 1000}\n')
    store.append_text("telemetry.jsonl", '{"event": "task_result", "outcome": "passed", "tokens_spent": 3000}\n')
    assert Knowledge(ArtifactStore(tmp_path / "other")).avg_task_tokens() == (2000.0, 2)
