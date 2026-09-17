#!/usr/bin/env python3
"""
segment_kmeans.py
------------------
Real machine-learning customer segmentation for PharmaLink, powered by
scikit-learn's K-Means clustering (sklearn.cluster.KMeans).

Contract (stdin -> stdout, both JSON):
  IN:
    {
      "ks": [2, 3, 4, 5],
      "customers": [
        {"customer_id": 4, "monetary": 473.0, "frequency": 5, "recency_days": 12},
        {"customer_id": 7, "monetary": 0.0, "frequency": 0, "recency_days": 9999},
        ...
      ]
    }
    Three RFM-style features are used per customer:
      - monetary:     total amount spent (from completed/placed orders)
      - frequency:    number of orders placed
      - recency_days: days since the customer's most recent order
                      (a large placeholder, e.g. 9999, is used for
                      customers who have never ordered)

    All requested k values are clustered in this ONE process run (the
    feature matrix is only built/normalized once and reused for every
    k) — this used to be one subprocess call per k (2, 3, 4, 5), which
    meant paying Python + numpy/sklearn's startup cost 4 times on every
    single page load. Batching into one call cuts that overhead ~4x.

  OUT (success):
    {"success": true,
     "results": {
        "2": {"success": true, "silhouette_score": 0.71, "cluster_order": [1, 0], "assignments": {...}, "cluster_stats": {...}},
        "3": {"success": true, ...},
        "4": {"success": false, "error": "..."},
        "5": {"success": false, "error": "..."}
     },
     "engine": "kmeans"}

    Each entry under "results" follows the same per-k shape as before.
    A given k can fail independently (e.g. not enough customers for
    that many clusters) while others succeed — the PHP caller falls
    back to quantile grouping only for the k values that failed.

  OUT (top-level failure): {"success": false, "error": "..."}  (exit code 1)
    Only used for input-level problems (bad JSON, missing/failed
    numpy+scikit-learn import) that would affect every k the same way.
"""
import sys
import json


def fail(msg):
    print(json.dumps({"success": False, "error": msg}))
    sys.exit(1)


def cluster_one_k(k, X, X_norm, ids):
    """Runs K-Means for a single k against an already-normalized feature
    matrix. Returns a per-k result dict (see module docstring)."""
    if len(ids) < k:
        return {"success": False, "error": f"Need at least {k} customers to form {k} clusters, got {len(ids)}"}

    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    import numpy as np

    try:
        model = KMeans(n_clusters=k, n_init=10, random_state=42)
        labels = model.fit_predict(X_norm)
    except Exception as e:
        return {"success": False, "error": f"KMeans failed: {e}"}

    silhouette = None
    try:
        if len(set(labels.tolist())) >= 2:
            silhouette = float(silhouette_score(X_norm, labels))
    except Exception:
        silhouette = None  # non-fatal — just omit it from the response

    cluster_stats = {}
    for cid in range(k):
        mask = labels == cid
        count = int(mask.sum())
        avg_monetary = float(X[mask, 0].mean()) if count > 0 else 0.0
        cluster_stats[str(cid)] = {"avg_monetary": round(avg_monetary, 2), "count": count}

    cluster_order = sorted(
        range(k),
        key=lambda cid: cluster_stats[str(cid)]["avg_monetary"],
        reverse=True,
    )

    assignments = {str(cust_id): int(lbl) for cust_id, lbl in zip(ids, labels.tolist())}

    return {
        "success": True,
        "silhouette_score": round(silhouette, 4) if silhouette is not None else None,
        "cluster_order": cluster_order,
        "assignments": assignments,
        "cluster_stats": cluster_stats,
    }


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        fail(f"Invalid JSON input: {e}")
        return

    ks = payload.get("ks")
    if ks is None and isinstance(payload.get("k"), int):
        ks = [payload["k"]]  # backwards-compatible single-k input
    if not isinstance(ks, list) or not ks or any((not isinstance(k, int) or k < 2) for k in ks):
        fail("ks must be a non-empty list of integers >= 2")
        return

    customers = payload.get("customers") or []
    if not customers:
        fail("No customers provided")
        return

    try:
        import numpy as np
        import sklearn  # noqa: F401  (import check only; used lazily per-k above)
    except ImportError as e:
        fail(f"scikit-learn / numpy not available: {e}")
        return

    ids = [c["customer_id"] for c in customers]
    X = np.array(
        [[float(c.get("monetary", 0)), float(c.get("frequency", 0)), float(c.get("recency_days", 0))]
         for c in customers],
        dtype=float,
    )

    # Z-score normalization — computed ONCE and reused for every k — so
    # Monetary (₱ hundreds/thousands) doesn't dominate Frequency (single
    # digits) or Recency (days) purely due to scale. Same normalization
    # approach used in the segmentation literature this study is based
    # on (e.g. Tabianan et al., 2022).
    means = X.mean(axis=0)
    stds = X.std(axis=0)
    stds[stds == 0] = 1.0  # avoid divide-by-zero for a constant column
    X_norm = (X - means) / stds

    no_variation = bool(np.allclose(X_norm, X_norm[0]))

    results = {}
    for k in ks:
        if no_variation:
            results[str(k)] = {"success": False, "error": "Not enough variation in customer data to form meaningful clusters"}
        else:
            results[str(k)] = cluster_one_k(k, X, X_norm, ids)

    print(json.dumps({"success": True, "results": results, "engine": "kmeans"}))


if __name__ == "__main__":
    main()