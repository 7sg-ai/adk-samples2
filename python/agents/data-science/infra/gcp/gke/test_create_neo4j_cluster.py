from pathlib import Path


def test_cluster_script_matches_locked_shape():
    text = Path("infra/gcp/gke/create_neo4j_cluster.sh").read_text(encoding="utf-8")
    assert "data-science-neo4j" in text
    assert "--zone us-central1-a" in text
    assert "--machine-type e2-standard-2" in text
    assert "--num-nodes 1" in text
    assert "--enable-ip-alias" in text
    assert "--network default" in text
    assert "--subnetwork default" in text
    assert "neo4j-bolt-ip" in text
    assert "SHARED_LOADBALANCER_VIP" in text
    assert "--vpc-egress private-ranges-only" in text
    assert "spot" not in text.lower()
    assert "--type public" not in text
