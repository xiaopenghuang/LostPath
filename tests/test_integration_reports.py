from lostpath import integration_reports


def test_reports_are_reused_for_same_data_revision(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(integration_reports.time, "monotonic", lambda: now[0])
    integration_reports.invalidate()
    calls = {"environment": 0, "registry": 0, "context": 0}

    def build(name):
        def run(_entities):
            calls[name] += 1
            return {"items": [name]}
        return run

    environment = build("environment")
    registry = build("registry")
    context = build("context")
    entities = [{"id": "demo"}]

    first = integration_reports.get(entities, environment, registry, context)
    second = integration_reports.get(entities, environment, registry, context)

    assert first is second
    assert calls == {"environment": 1, "registry": 1, "context": 1}


def test_cache_expires_and_can_be_invalidated(monkeypatch):
    now = [20.0]
    monkeypatch.setattr(integration_reports.time, "monotonic", lambda: now[0])
    integration_reports.invalidate()
    calls = []

    def build(_entities):
        calls.append(now[0])
        return {"items": []}

    entities = []
    integration_reports.get(entities, build, build, build)
    now[0] += integration_reports.CACHE_SECONDS + 0.1
    integration_reports.get(entities, build, build, build)
    integration_reports.invalidate()
    integration_reports.get(entities, build, build, build)

    assert len(calls) == 9
