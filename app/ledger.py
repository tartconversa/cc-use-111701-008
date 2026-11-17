"""落地链路台账。

台账按门店/物料分组，每组的记录分工明确：

- **路径证据**：每个二维码的每次领取都单独一行 ``scans``，包含它自己的
  跳转序列和停止位置。不同二维码即使到达同一最终内容，路径也不合并。
- **内容观测**：``observations`` 记录该二维码每次看到的最终内容身份。
  目标换版后，旧记录的 ``valid_until`` 与 ``superseded_by`` 指出旧链路何时
  失效；``observations`` 通过 ``shared_by`` 报告“哪些二维码共享同一最终
  页面”，但各自的 ``scans`` 仍是独立证据。

``save``/``load`` 使用单个 JSON 文件，先写临时文件再原子替换。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1


def receipt(response, items):
    """原有单响应收据：响应身份与提取条目。"""
    return {
        "source_id": response.source_id,
        "url": response.url,
        "response_sha256": response.sha256,
        "items": items,
    }


def empty():
    return {"schema_version": SCHEMA_VERSION, "chains": {}}


def _chain_key(store_id, qr_id):
    return f"{store_id}/{qr_id}"


def chain(db, store_id, qr_id):
    """取得或创建某门店/物料的链路记录。"""
    if not isinstance(db.get("chains"), dict):
        raise ValueError("台账格式无效")
    key = _chain_key(store_id, qr_id)
    value = db["chains"].get(key)
    if value is None:
        value = {"store_id": store_id, "qr_id": qr_id, "scans": [], "observations": []}
        db["chains"][key] = value
    return value


def record_scan(db, result):
    """把一次采集结果写入台账，返回对应的扫描记录。

    未完成的采集同样写入。``verified_hops``/``pending_hop`` 让调用者分清已核实
    的跳与未完成的跳转；它们不会更新 ``observations``。
    """
    store_id = result.get("store_id")
    qr_id = result.get("qr_id")
    if not store_id or not qr_id:
        raise ValueError("采集结果缺少门店/物料身份")
    entry = chain(db, store_id, qr_id)
    scan_id = result.get("scan_id")
    if scan_id is not None and any(existing["scan_id"] == scan_id for existing in entry["scans"]):
        raise ValueError("扫描编号重复")
    previous = entry["scans"][-1]["scan_id"] if entry["scans"] else None
    scan = {
        "scan_id": scan_id,
        "attempt": len(entry["scans"]) + 1,
        "retry_of": previous,
        "collected_at": result.get("collected_at"),
        "outcome": result.get("outcome"),
        "stop_reason": result.get("stop_reason"),
        "failure": result.get("failure"),
        "hops": result.get("hops", []),
        "path": list(
            result.get("path")
            or [
                hop.get("response_url") or hop.get("request_url")
                for hop in result.get("hops", [])
            ]
        ),
        "verified_hops": result.get("verified_hops", 0),
        "pending_hop": result.get("pending_hop"),
        "final_content": result.get("final_content"),
        "viewport": result.get("viewport"),
        "allowed_domains": result.get("allowed_domains"),
        "printed_url": result.get("printed_url"),
    }
    entry["scans"].append(scan)
    if result.get("outcome") != "complete" or not result.get("final_content"):
        return scan
    _observe(entry, scan)
    _mark_shared(db)
    return scan


def _observe(entry, scan):
    content = scan["final_content"]
    observed_at = scan.get("collected_at")
    current = next((item for item in entry["observations"] if item["valid_until"] is None), None)
    same = (
        current is not None
        and current["identity"]["kind"] == content["kind"]
        and current["identity"]["value"] == content["value"]
    )
    if same:
        current["scan_ids"].append(scan["scan_id"])
        current["last_seen_at"] = observed_at
        return
    if current is not None:
        current["valid_until"] = observed_at
        current["invalidated_by"] = scan["scan_id"]
    observation = {
        "version": len(entry["observations"]) + 1,
        "identity": content,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "valid_until": None,
        "supersedes": current["version"] if current is not None else None,
        "superseded_by": None,
        "invalidated_by": None,
        "scan_ids": [scan["scan_id"]],
    }
    entry["observations"].append(observation)
    if current is not None:
        current["superseded_by"] = observation["version"]


def _mark_shared(db):
    """标注哪些二维码当前共享同一最终内容身份。

    只写 ``shared_by`` 这一派生字段；各二维码的 ``scans`` 保持独立。
    """
    by_identity = {}
    for key, entry in db["chains"].items():
        current = next((item for item in entry["observations"] if item["valid_until"] is None), None)
        if current is None:
            continue
        identity = (current["identity"]["kind"], current["identity"]["value"])
        by_identity.setdefault(identity, []).append(key)
    for key, entry in db["chains"].items():
        for observation in entry["observations"]:
            identity = (observation["identity"]["kind"], observation["identity"]["value"])
            observation["shared_by"] = sorted(by_identity.get(identity, []))


def completed_paths(entry):
    """已核实完成的链路；每次采集分别列出。"""
    return [
        {
            "scan_id": scan["scan_id"],
            "collected_at": scan["collected_at"],
            "path": scan["path"],
            "final_content": scan["final_content"],
        }
        for scan in entry["scans"]
        if scan["outcome"] == "complete"
    ]


def pending_scans(entry):
    """未完成或需要重试的采集。"""
    return [
        {
            "scan_id": scan["scan_id"],
            "collected_at": scan["collected_at"],
            "outcome": scan["outcome"],
            "stop_reason": scan["stop_reason"],
            "failure": scan["failure"],
            "verified_hops": scan["verified_hops"],
            "pending_hop": scan["pending_hop"],
            "path": scan["path"],
        }
        for scan in entry["scans"]
        if scan["outcome"] != "complete"
    ]


def save(path, db):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(db, ensure_ascii=False, indent=2) + "\n"
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return path


def load(path):
    path = Path(path)
    if not path.exists():
        return empty()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("台账格式无效")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("台账版本不支持")
    if not isinstance(data.get("chains"), dict):
        raise ValueError("台账格式无效")
    return data
