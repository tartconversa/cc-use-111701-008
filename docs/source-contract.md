# 来源契约

登记表含 qr_id、store_id、printed_url 和允许的域名集合；演练站点提供 HTTP 3xx、HTML meta refresh 及 DOM 导航三种方式。每跳记录请求 URL、响应 URL、状态、Location 或导航触发、实体摘要与时间；不会访问白名单外页面。移动视口配置冻结，页面内容可换版。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 qr_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
