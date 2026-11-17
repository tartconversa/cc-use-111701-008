# 来源契约

查询输入为行政代码和闭合矩形；响应含 query_id、limit、stores[{store_id,address,lon,lat}]，可能有 next_page 或 total_hint，缺失时不代表完整。行政区树与授权界限由配置提供；相邻矩形边界可能重复。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 store_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
