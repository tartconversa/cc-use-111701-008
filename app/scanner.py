"""扫码到落地链路的运行时入口（供巡店 App / 命令行调用）。

运行时提供两点：登记表（来源）与响应来源（真实 HTTP 或离线映射）。领取时
可以附带扫码得到的 URL；与登记不一致会直接拒绝，避免把别的二维码的材料
记到这个物料编号名下。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .landing import collect_landing
from .ledger import chain, empty, load, record_scan, save
from .registry import Registry
from .report import chain_report
from .transport import HttpTransport, MappingTransport


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def transport_from(mapping_path=None):
    if mapping_path:
        return MappingTransport.from_file(mapping_path)
    return HttpTransport()


def scan(registry, transport, store_id, qr_id, *, scanned_url=None, ledger_path=None, scan_id=None, collected_at=None):
    """领取并采集一条链路，返回 ``(结果, 台账视图)``。"""
    registration = registry.claim(store_id, qr_id, scanned_url=scanned_url)
    result = collect_landing(transport, registration, scan_id=scan_id or uuid.uuid4().hex)
    result["collected_at"] = collected_at or _now()
    db = load(ledger_path) if ledger_path else empty()
    record_scan(db, result)
    if ledger_path:
        save(ledger_path, db)
    return result, chain_report(chain(db, store_id, qr_id))


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="采集一条二维码落地链路")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--qr", required=True)
    parser.add_argument("--scanned-url", help="扫码得到的 URL；必须与登记一致")
    parser.add_argument("--ledger")
    parser.add_argument("--mapping", help="离线演练站点映射")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result, report = scan(
        Registry.from_file(args.registry),
        transport_from(args.mapping),
        args.store,
        args.qr,
        scanned_url=args.scanned_url,
        ledger_path=args.ledger,
    )
    print(json.dumps({"result": result, "chain": report}, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
