from infra.gcp.neo4j.provision import provision


class FakeNeo4jAdapter:
    def __init__(self):
        self.installed = None
        self.stored = None

    def cluster_exists(self, project_id, zone, cluster_name):
        return True

    def install(self, plan):
        self.installed = plan

    def bolt_ready(self, plan):
        return True

    def put_password(self, project_id, secret_id, password):
        self.stored = (project_id, secret_id, password)

    def uninstall(self, plan):
        raise AssertionError("present must not uninstall")


def _request(**overrides):
    payload = {
        "protocolVersion": "1",
        "desiredState": "present",
        "runId": "run-1",
        "projectId": "gen-lang-client-0373235205",
        "runtimeServiceAccount": "deploy@gen-lang-client-0373235205.iam.gserviceaccount.com",
        "zone": "us-central1-a",
        "clusterName": "data-science-neo4j",
        "namespace": "neo4j",
        "releaseName": "neo4j",
        "loadBalancerIp": "10.128.0.20",
        "storageGb": 20,
        "chartVersion": "5.26.1",
    }
    payload.update(overrides)
    return payload


def test_present_installs_internal_neo4j_and_hides_password():
    adapter = FakeNeo4jAdapter()
    payload = provision(_request(), adapter)
    assert payload["status"] == "succeeded"
    assert payload["environment"] == {
        "NEO4J_URI": "bolt://10.128.0.20:7687",
        "NEO4J_USER": "neo4j",
    }
    assert adapter.stored[1] == "neo4j-password"
    assert adapter.stored[2]
    rendered = str(payload)
    assert adapter.stored[2] not in rendered
    plan = adapter.installed
    assert plan["chart"] == "neo4j/neo4j"
    assert plan["chart_version"] == "5.26.1"
    assert plan["values"]["services"]["neo4j"]["spec"]["type"] == "LoadBalancer"
    assert plan["values"]["services"]["neo4j"]["spec"]["loadBalancerIP"] == "10.128.0.20"
    assert plan["values"]["services"]["neo4j"]["annotations"]["networking.gke.io/load-balancer-type"] == "Internal"
    assert plan["values"]["volumes"]["data"]["dynamic"]["requests"]["storage"] == "20Gi"
    assert adapter.stored[0] == "gen-lang-client-0373235205"
    assert plan["values"]["neo4j"]["edition"] == "community"
    assert "password" not in str(plan["values"])
