# 财报点评质量清单

- 章节必须覆盖 summary、performance、quality、outlook、risks、sources，无证据章节明确显示缺口，不用空泛判断充数。
- 所有事实段落含原始 locator/title/page 或文本行定位；观点归属机构并标日期。
- 数字使用上游结果；metrics.pointers 为 JSON Pointer，必须逐项绑定 value、unit、period、scope、references，涉及币种还须 currency。
- 无法验证字段路径或数值/单位/期间/口径不一致时校验失败；缺失值需 missing_reason。
- 政策已生效与征求意见、需求与出货、名义与实际、存量与新增不可混用。
- 公开原文不可用或只有摘要须列缺口；缺少证据不能标 complete；没有任何证据须 blocked。
- 缺少前置 Skill 时不自行启用、不读取其脚本绕过开关。权限由工具运行时决定。
- 定量字段由脚本校验；自由文本论述需逐段核对原文，不能声称软件已验证所有判断。

财务比率沿用上游 dimensionless 契约：unit=ratio，可省略 unit 指针；0.4 是比率，不是 0.4%。其余口径及引用必须有字段路径。
