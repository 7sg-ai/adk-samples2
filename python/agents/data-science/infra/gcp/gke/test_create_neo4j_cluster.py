from pathlib import Path


def test_cluster_script_matches_locked_shape():
    text = Path("infra/gcp/gke/create_neo4j_cluster.sh").read_text(encoding="utf-8")
    assert "data-science-neo4j" in text
    assert "--zone us-central1-a" in text
    assert "--machine-type e2-standard-2" in text
    assert "--num-nodes 1" in text
    assert "--enable-ip-alias" in text
    assert "--network data-science-neo4j" in text
    assert "--subnetwork data-science-neo4j" in text
    assert "--subnet data-science-neo4j" in text
    assert "--range=10.128.0.0/28" in text
    assert "pods=10.128.1.0/24" in text
    assert "services=10.128.2.0/27" in text
    assert "--network default" not in text
    assert "--subnetwork default" not in text
    assert "neo4j-bolt-ip" in text
    assert "SHARED_LOADBALANCER_VIP" in text
    assert "--vpc-egress private-ranges-only" in text
    assert "spot" not in text.lower()
    assert "--type public" not in text
