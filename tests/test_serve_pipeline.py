"""Regression tests for the serve/worker pipeline in offline (simulated) mode."""

import os
import re

import pytest

from solvent.jobs import SAMPLE_JOBS

ENV = {
    "SOLVENT_DELIVERY_SECRET": "x" * 32,
    "SOLVENT_DASHBOARD_TOKEN": "d" * 32,
}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLVENT_HOME", str(tmp_path))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def test_api_job_submission_does_not_recurse(isolated_home):
    """The server's event handler used to call back into agent._capture_event,
    which re-invoked the handler forever (RecursionError -> HTTP 500)."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("FastAPI test client is not installed")
    from solvent.server import create_app

    client = TestClient(create_app(fresh=True))
    response = client.post("/jobs", json=SAMPLE_JOBS[0])
    assert response.status_code == 200
    body = response.json()
    assert body.get("simulated") is True
    assert body.get("url", "").startswith("https://checkout.stripe.test/")

    status = client.get("/api/status", params={"token": ENV["SOLVENT_DASHBOARD_TOKEN"]})
    assert status.status_code == 200
    assert SAMPLE_JOBS[0]["id"] in status.json()["jobs_data"]


def test_worker_confirms_simulated_payment_without_webhook(isolated_home):
    """In async mode with no live Stripe key, a simulated checkout can never
    receive a webhook, so the worker must confirm it the way the sync path does."""
    from solvent.agent import Solvent

    assert not os.environ.get("STRIPE_SECRET_KEY")
    agent = Solvent(seed_cents=10_000, fresh=True, sync_payment=False)
    job = SAMPLE_JOBS[0]

    queued = agent.enqueue_job(job)
    assert queued.get("simulated") is True
    assert agent.t.get_job(job["id"])["status"] == "awaiting_payment"

    result = agent.advance_job(job["id"])
    assert result.get("stage") != "payment_pending", result
    assert agent.t.get_job(job["id"])["status"] == "completed"
    assert agent.t.job_has_revenue(job["id"])
    assert agent.t.balance_cents() > 10_000


def test_dashboard_script_has_no_literals_split_across_lines(isolated_home):
    """A raw newline inside a JS regex or string literal is a syntax error that
    disables the whole dashboard script (briefs list, modal, live updates)."""
    from solvent import dashboard
    from solvent.agent import Solvent

    agent = Solvent(seed_cents=10_000, fresh=True, sync_payment=False)
    html = dashboard.render(agent.t.snapshot(), []).read_text(encoding="utf-8")

    script = html[html.index("<script>") : html.rindex("</script>")]
    assert "split(/\\n\\n+/)" in script
    assert "replace(/\\n/g" in script
    assert ".join('\\n')" in script
    # No line may end inside an unterminated regex literal or string literal.
    assert not re.search(r"(split|replace)\(/\s*$", script, re.M)
    assert not re.search(r"\.join\('\s*$", script, re.M)
