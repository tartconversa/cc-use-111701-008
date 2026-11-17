"""面向巡店人员的链路报告。

报告把三件事分开呈现：

- ``completed``：已核实的完整路径（每一跳的方式与授权域名）。
- ``pending``：未完成的采集，说明停在哪一步、哪个 URL、为什么。
- ``versions``：同一物料目标换版的历史，``valid_until`` 指出旧链路何时失效。

内容身份相同的二维码仍各自列出路径；报告不会按最终内容合并路径证据。
"""
from __future__ import annotations

from .ledger import completed_paths


def _transition_label(hop):
    transition = hop.get("transition")
    if not transition:
        return None
    return transition.get("kind")


def _transition_authorized(hop):
    transition = hop.get("transition")
    if not transition:
        return None
    return transition.get("authorized")


def summarize_scan(scan):
    return {
        "scan_id": scan["scan_id"],
        "attempt": scan.get("attempt"),
        "retry_of": scan.get("retry_of"),
        "collected_at": scan["collected_at"],
        "outcome": scan["outcome"],
        "stop_reason": scan["stop_reason"],
        "failure": scan["failure"],
        "verified_hops": scan["verified_hops"],
        "pending_hop": scan["pending_hop"],
        "path": [
            {
                "step": hop["step"],
                "request_url": hop["request_url"],
                "response_url": hop["response_url"],
                "status": hop["status"],
                "transition": _transition_label(hop),
                "transition_target": (hop.get("transition") or {}).get("to_url"),
                "transition_authorized": _transition_authorized(hop),
                "matched_domain": (hop.get("authorization") or {}).get("matched_domain"),
                "content_identity": (hop.get("content") or {}).get("value"),
            }
            for hop in scan.get("hops", [])
        ],
    }


def chain_report(entry):
    """生成单个门店/物料的报告。"""
    return {
        "store_id": entry["store_id"],
        "qr_id": entry["qr_id"],
        "completed": [summarize_scan(scan) for scan in entry["scans"] if scan["outcome"] == "complete"],
        "pending": [summarize_scan(scan) for scan in entry["scans"] if scan["outcome"] != "complete"],
        "versions": [
            {
                "version": item["version"],
                "identity": item["identity"]["value"],
                "identity_kind": item["identity"]["kind"],
                "first_seen_at": item["first_seen_at"],
                "last_seen_at": item["last_seen_at"],
                "valid_until": item["valid_until"],
                "supersedes": item["supersedes"],
                "superseded_by": item.get("superseded_by"),
                "scan_ids": list(item["scan_ids"]),
                "shared_by": list(item.get("shared_by", [])),
            }
            for item in entry["observations"]
        ],
        "paths": completed_paths(entry),
    }


def store_report(db, store_id):
    """门店报告：逐个二维码列出，再标注共享内容。"""
    chains = [
        chain_report(entry)
        for entry in db["chains"].values()
        if entry["store_id"] == store_id
    ]
    chains.sort(key=lambda item: item["qr_id"])
    shared = {}
    for item in chains:
        for version in item["versions"]:
            if version["valid_until"] is None:
                shared.setdefault(version["identity"], []).append(item["qr_id"])
    return {
        "store_id": store_id,
        "chains": chains,
        "shared_identities": [
            {"identity": identity, "qr_ids": sorted(qr_ids)}
            for identity, qr_ids in sorted(shared.items())
            if len(qr_ids) > 1
        ],
    }


def batch_report(results):
    """一批采集的摘要；不同二维码的路径并排保留。"""
    return {
        "store_id": results["store_id"],
        "chains": [
            {
                "qr_id": result["qr_id"],
                "scan_id": result["scan_id"],
                "outcome": result["outcome"],
                "stop_reason": result["stop_reason"],
                "failure": result["failure"],
                "verified_hops": result["verified_hops"],
                "pending_hop": result["pending_hop"],
                "path": [
                    {
                        "step": hop["step"],
                        "request_url": hop["request_url"],
                        "response_url": hop["response_url"],
                        "status": hop["status"],
                        "transition": _transition_label(hop),
                        "transition_target": (hop.get("transition") or {}).get("to_url"),
                        "transition_authorized": _transition_authorized(hop),
                        "matched_domain": (hop.get("authorization") or {}).get("matched_domain"),
                        "content_identity": (hop.get("content") or {}).get("value"),
                    }
                    for hop in result["hops"]
                ],
                "final_content": result["final_content"],
            }
            for result in results["results"]
        ],
    }
