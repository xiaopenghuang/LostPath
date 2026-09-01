from lostpath.software_identity import (
    match_registry_entity,
    normalize_name,
    normalize_publisher,
    relate_environment_variable,
)


def _entity(**overrides):
    entity = {
        "id": "r:acme:demoeditor",
        "name": "Demo Editor",
        "publisher": "Acme Corporation",
        "location": r"G:\Apps\Demo Editor",
        "exe_path": r"G:\Apps\Demo Editor\demo.exe",
        "icon": "/icons/demo.png",
        "fragments": [],
        "redirects": [],
    }
    entity.update(overrides)
    return entity


def test_registry_relation_uses_same_identity_as_inventory():
    relation = match_registry_entity({
        "name": "Demo Editor 2.4.1",
        "publisher": "Acme Corp",
        "location": r"G:\Apps\Demo Editor",
    }, [_entity()])

    assert normalize_name("Demo Editor 2.4.1") == "demoeditor"
    assert normalize_publisher("Acme Corporation") == "acme"
    assert relation == {
        "entity_id": "r:acme:demoeditor",
        "name": "Demo Editor",
        "publisher": "Acme Corporation",
        "icon": "/icons/demo.png",
        "reason": "软件名与发布商和台账登记一致",
        "confidence": 1.0,
    }


def test_registry_relation_can_follow_aggregated_component():
    host = _entity(fragments=["Demo Runtime 2.0"])
    relation = match_registry_entity({
        "name": "Demo Runtime 2.0",
        "publisher": "Acme",
    }, [host])

    assert relation["entity_id"] == host["id"]
    assert relation["confidence"] == 0.94
    assert "组件" in relation["reason"]


def test_registry_relation_refuses_ambiguous_name_only_match():
    entities = [
        _entity(id="r:one:player", name="Player", publisher=None, location=None),
        _entity(id="r:two:player", name="Player", publisher=None, location=None),
    ]
    assert match_registry_entity({"name": "Player", "publisher": None}, entities) is None


def test_environment_relation_uses_redirect_declaration_and_path_evidence():
    cache = _entity(
        id="t:uv",
        name="uv",
        publisher=None,
        location=None,
        exe_path=None,
        redirects=["UV_CACHE_DIR"],
    )
    other = _entity(
        id="r:acme:helper",
        name="Acme Helper",
        location=r"G:\Apps\Helper",
        exe_path=None,
    )

    redirect = relate_environment_variable(
        "UV_CACHE_DIR", r"G:\Caches\uv", [cache, other])
    path = relate_environment_variable(
        "PATH", r"C:\Windows;G:\Apps\Helper\bin", [cache, other])

    assert [(item["entity_id"], item["confidence"]) for item in redirect] == [("t:uv", 1.0)]
    assert [(item["entity_id"], item["confidence"]) for item in path] == [
        ("r:acme:helper", 0.98),
    ]


def test_sensitive_environment_value_is_never_used_as_path_evidence():
    relations = relate_environment_variable(
        "DEMO_TOKEN", r"G:\Apps\Demo Editor\token", [_entity()], sensitive=True)
    assert relations == []
