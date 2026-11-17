# 来源契约

登记表含 qr_id、store_id、printed_url 和允许的域名集合；演练站点提供 HTTP 3xx、HTML meta refresh 及 DOM 导航三种方式。每跳记录请求 URL、响应 URL、状态、Location 或导航触发、实体摘要与时间；不会访问白名单外页面。移动视口配置冻结，页面内容可换版。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 qr_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。

## 落地链路采集

单响应流程保持不变。按登记任务采集时：

- 登记表由运行时提供，字段为 `qr_id`、`store_id`、`printed_url`、`allowed_domains`；可选 `viewport`（条目级）或顶层 `viewports` 数组。样例见 `fixtures/registry.json`。
- 领取必须同时给出已登记的 `store_id` 和 `qr_id`。扫码 URL 可附带校验，必须与 `printed_url` 一致（忽略 fragment、默认端口与主机大小写）。
- 只允许 http/https。`allowed_domains` 中的条目匹配自身与子域，`*.example.test` 只匹配子域；`badexample.test` 不属于 `example.test`。
- 逐跳记录请求 URL、响应 URL、状态、响应头摘要、`body_sha256`、`transition`（`http_redirect` / `meta_refresh` / `script_navigation` / `dom_navigation`）和命中的登记域名。`transition.authorized=false` 明确表示该目标不是自家活动页。
- 循环（规范化 URL 重复，含刷新自身与 `location.reload()`）、越界、预算耗尽、无法静态解析或互相竞争的导航意图都会停止链路，`failure` 给出 `step`、`url`、触发的 `signal`。跨站目标不会被请求，也不会被当作最终页。
- 完成判定只看“文档中是否还有待执行的导航意图”，与页面标题无关；`meta refresh`/`setTimeout` 的声明延时会记录，但不会据此阻塞等待。
- 识别的导航来源包括：`meta refresh`、`<base href>` 解析后的相对目标、`location` 赋值/`replace`/`assign`/`reload`、`window.open`、内联 `onload`/`onpageshow`、`data-auto-navigate` 元素，以及 `getElementById(...).click()` 指向的已登记元素。`form.submit()`、`querySelector(...).click()`、变量拼接的目标等无法静态判定的情况报 `ambiguous_navigation`。只有外部脚本、没有内联可判定信号的页面报 `external_script` 未完成，不当作最终页。
- 最终内容身份优先取响应头 `X-Content-Id`/`X-Campaign-Id`，其次文档的 `meta[name=campaign-id]`、`data-content-id`；都没有时用实体 SHA-256。相同身份表示同一最终页面，但每个二维码的 `hops`/`path` 始终单独保存。
- 台账是单个 JSON 文件（`ledger.json`），按 `store/qr` 分组。`scans[].attempt`/`retry_of` 标出重试关系，`verified_hops` 与 `pending_hop` 区分已核实的跳和未完成的跳转。`observations[].valid_until` 标出旧内容何时被换版取代，`shared_by` 标出共享同一最终页面的二维码。
- 离线演练站点用映射文件（样例 `fixtures/site.json`）替代真实请求；映射键按规范化 URL 匹配，所有实际请求记录在传输对象的 `requests` 中。
