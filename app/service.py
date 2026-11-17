"""采集入口。

保留原有单响应入口 ``inspect``/``collect_one``，并增加按登记任务领取二维码、
逐跳采集落地链路的入口。命令行用法：

    python -m app.service fixtures/sample.json
    python -m app.service --registry registry.json --ledger ledger.json \
        --store s-1 --qr qr-1 [--qr qr-2 ...] [--mapping site.json]

``--mapping`` 指向离线演练站点映射；不给出时使用真实 HTTP 传输。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .extract import extract
from .landing import LandingCollector
from .ledger import empty as empty_ledger, load as load_ledger, record_scan, save as save_ledger
from .ledger import receipt, chain as ledger_chain
from .report import chain_report, store_report
from .registry import Registry
from .source import fetch, from_packet
from .transport import HttpTransport, MappingTransport

KEY = "qr_id"


def inspect(packet):
    response = from_packet(packet)
    return receipt(response, extract(response, KEY))


def collect_one(url, source_id):
    response = fetch(url, source_id)
    return receipt(response, extract(response, KEY))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def collect_chain(registry, store_id, qr_id, transport, *, scan_id=None, collected_at=None):
    """领取一个登记任务并采集其落地链路。"""
    registration = registry.claim(store_id, qr_id)
    collector = LandingCollector(transport)
    result = collector.collect(
        registration,
        scan_id=scan_id or uuid.uuid4().hex,
    )
    result["collected_at"] = collected_at or _now()
    return result


def run_batch(registry, transport, *, store_id, qr_ids, ledger_path=None, collected_at=None):
    """按门店与物料编号采集一批二维码，并把结果写入台账。

    ``qr_ids`` 为 ``None`` 时领取该门店的全部物料编号。返回本次结果与台账
    的完成/待重试视图。
    """
    if qr_ids is None:
        qr_ids = [item["qr_id"] for item in registry.items(store_id)]
    if not qr_ids:
        raise ValueError("没有可采集的物料编号")
    results = []
    for qr_id in qr_ids:
        results.append(
            collect_chain(registry, store_id, qr_id, transport, collected_at=collected_at)
        )
    db = load_ledger(ledger_path) if ledger_path else empty_ledger()
    for result in results:
        record_scan(db, result)
    if ledger_path:
        save_ledger(ledger_path, db)
    chains = [
        chain_report(ledger_chain(db, store_id, result["qr_id"])) for result in results
    ]
    return {
        "store_id": store_id,
        "results": [{**result, "chain": summary} for result, summary in zip(results, chains)],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="二维码落地链路采集")
    parser.add_argument("packet", nargs="?", help="单响应来源文件")
    parser.add_argument("--registry", help="登记表文件")
    parser.add_argument("--ledger", help="台账文件")
    parser.add_argument("--store", help="门店编号")
    parser.add_argument("--qr", action="append", dest="qrs", help="物料编号，可重复")
    parser.add_argument("--mapping", help="离线演练站点映射文件")
    parser.add_argument("--json", action="store_true", help="以紧凑 JSON 输出")
    parser.add_argument("--report", action="store_true", help="输出门店报告（需配合 --ledger）")
    args = parser.parse_args(argv)

    chained = args.registry or args.store or args.qrs
    if args.packet and (chained or args.report):
        parser.error("单响应入口与登记采集入口不能同时使用")
    if args.report:
        if not (args.ledger and args.store):
            parser.error("门店报告需要 --ledger 与 --store")
        payload = store_report(load_ledger(args.ledger), args.store)
    elif args.packet:
        payload = inspect(json.loads(Path(args.packet).read_text(encoding="utf-8")))
    else:
        if not (args.registry and args.store):
            parser.error("登记采集需要 --registry 与 --store")
        registry = Registry.from_file(args.registry)
        transport = MappingTransport.from_file(args.mapping) if args.mapping else HttpTransport()
        payload = run_batch(
            registry,
            transport,
            store_id=args.store,
            qr_ids=args.qrs,
            ledger_path=args.ledger,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main(sys.argv[1:])
