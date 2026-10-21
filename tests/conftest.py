import json
import os
import sys
from pathlib import Path

# Tests never call a real LLM: force the offline provider before the app is imported.
os.environ["LLM_PROVIDER"] = "mock"
os.environ.setdefault("LOG_LEVEL", "WARNING")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app.verified_data import get_verified_data  # noqa: E402

SCENARIOS = {s["id"]: s for s in json.loads((ROOT / "app" / "data" / "scenarios.json").read_text(encoding="utf-8"))}


@pytest.fixture
def data():
    return get_verified_data()


@pytest.fixture
def scenario_text():
    return lambda sid: SCENARIOS[sid]["text"]


def draft_json(**overrides) -> str:
    """A minimal valid LLM draft, with fields overridden per test."""
    draft = {
        "issue_category": "other",
        "urgency": "low",
        "claims": [],
        "recommended_next_action": {"type": "request_more_info", "details": "Ask the rep for details."},
        "missing_information": [],
        "needs_human_review": False,
        "review_reason": "",
    }
    draft.update(overrides)
    return json.dumps(draft)
