PY ?= python

.PHONY: install run test eval eval-real schema docker

install:
	$(PY) -m pip install -r requirements-dev.txt

run:
	$(PY) -m uvicorn app.main:app --reload --port 8000

test:
	$(PY) -m pytest

eval:            ## rules baseline, no API key needed
	$(PY) -m evals.run --provider mock --label mock-baseline

eval-real:       ## real model, 3 runs per case; set LABEL=v1 / v2 ...
	$(PY) -m evals.run --provider anthropic --repeat 3 --label $(or $(LABEL),anthropic)

schema:
	$(PY) -c "import json; from app.schemas import TriageResult; open('docs/output_schema.json','w').write(json.dumps(TriageResult.model_json_schema(), indent=2) + '\n')"

docker:
	docker build -t saleseasy-copilot . && docker run --rm -p 8000:8000 --env-file .env saleseasy-copilot
