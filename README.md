# 门店物料二维码的落地链路采集

当前程序只接收一份来源 JSON 响应并生成条目收据。source.py 保存响应状态、头和原始字节；extract.py 读取现有 records 条目；ledger.py 绑定来源与响应摘要；service.py 提供单响应入口。项目使用 Python 标准库，不接触真实企业系统。现有单响应流程不证明跨页、跨资源或跨版本采集完整。

来源与字段约定见 docs/source-contract.md。

运行当前示例：

    python -m app.service fixtures/sample.json

按登记的门店和物料编号采集落地链路（离线演练站点）：

    python -m app.service --registry fixtures/registry.json \
        --mapping fixtures/site.json --store s-1 --ledger ledger.json

单条扫码采集（可校验扫码 URL）：

    python -m app.scanner --registry fixtures/registry.json \
        --mapping fixtures/site.json --store s-1 --qr qr-1 \
        --scanned-url https://m.example.test/q/1 --ledger ledger.json

查看门店报告（哪些二维码完成、哪些未完成、共享的最终内容与换版历史）：

    python -m app.service --ledger ledger.json --store s-1 --report

子命令说明：

- `app/scanner.py`：单条扫码采集入口，返回采集结果和该二维码的台账视图。
- `app/registry.py`：按 `store_id`/`qr_id` 领取登记任务，校验扫码 URL。
- `app/landing.py`：逐跳采集。HTTP 3xx、`meta refresh`、脚本导航和 DOM 自动导航可以交错出现；循环、越界和无法判定的导航会停下并给出位置。
- `app/htmlnav.py`：静态分析页面中的导航意图，不执行脚本、不按标题判断加载完成。
- `app/snapshot.py`：最终内容身份（显式声明优先，否则实体摘要）。
- `app/ledger.py`：路径证据与内容观测分列；重试保留 `attempt`/`retry_of`、`verified_hops`/`pending_hop`；换版用 `valid_until`/`superseded_by` 标记。
- `app/report.py`：门店报告，共享最终页面的二维码仍各自保留路径。

运行测试：

    python -m unittest discover -s tests -v

检查编译：

    python -m compileall -q app tests
