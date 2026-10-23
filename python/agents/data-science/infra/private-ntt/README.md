# private-ntt infrastructure

Kubernetes manifests and load configuration for the `private-ntt` scenario on
the NTT RKE2 cluster (`ntt-demo-prep`, namespace `ntt-tenant-a`).

## Components

| File | Purpose |
|------|---------|
| `neo4j.yaml` | Neo4j 5 StatefulSet + Service (Bolt 7687, HTTP 7474) |
| `postgres.yaml` | Postgres 16 Deployment + Service (5432) |
| `load.json` | Load specification the deploy Job executes after companions are ready |

## Data loading

The deploy toolchain runs a Kubernetes Job from the built app image after
the companion pods are ready. The Job executes:

```
python -m data_science.private_ntt_load
```

This loads `sample_data/Acme Corp Financial Model.xlsx` into both Postgres
(relational tables) and Neo4j (property graph). Environment variables
`POSTGRES_*` and `NEO4J_*` are injected from the scenario's secret refs.
