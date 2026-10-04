"""Every add-on surface registers on top of the bridge's own routes.

The Skills API once registered a view named `api_capabilities`, which the
bridge already had. Flask refused the whole module, the bridge swallowed the
error as a warning, and the window's Skills screen crashed on a route that did
not exist. Its own tests passed because they built a fresh Flask app. This
test rebuilds the bridge's real route table first, the way startup does.
"""
from flask import Flask


def _bridge_clone():
    from desk import bridge
    app = Flask("clone")
    for rule in bridge.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        app.add_url_rule(rule.rule, endpoint=rule.endpoint,
                         view_func=bridge.app.view_functions[rule.endpoint],
                         methods=rule.methods - {"HEAD", "OPTIONS"})
    return app


def _passthrough(fn):
    return fn


def test_every_surface_registers_without_colliding():
    app = _bridge_clone()
    from desk import account_api, knowledge_api, offline_model, skills_api
    from nova_core.rag import api as rag_api
    account_api.register(app, _passthrough, {})
    offline_model.register(app, _passthrough)
    rag_api.register(app, _passthrough)
    skills_api.register(app, _passthrough, sweep=False)
    knowledge_api.register(app, _passthrough)
    rules = {(r.rule, m) for r in app.url_map.iter_rules() for m in r.methods}
    for path, method in [("/api/skills", "GET"), ("/api/skills/check", "POST"),
                         ("/api/skills/<cap_id>/credential", "POST"),
                         ("/api/knowledge", "GET"), ("/api/knowledge/learn", "POST"),
                         ("/api/knowledge/domains/<domain_id>", "DELETE"),
                         ("/api/capabilities", "GET")]:
        assert (path, method) in rules, (path, method)


def test_the_bridge_says_so_loudly_when_a_surface_fails():
    src = open("desk/bridge.py", encoding="utf-8").read()
    for name in ("skills surface unavailable", "knowledge surface unavailable"):
        line = next(l for l in src.splitlines() if name in l)
        assert "log.error" in line, line
