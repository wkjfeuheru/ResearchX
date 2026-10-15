---
name: deep-investment-report
description: 消费财务、事件、研报及盈利预测结果，组织有证据的公司深度报告，校验关键数字、口径和引用。
skill_id: deep-investment-report
version: 1.0.0
owner: ResearchX
permissions: [file_read, network_read, file_write, shell_execution]
required_tools: [skill, read_file, write_file, bash]
optional_tools: [research_memory, web_search, web_fetch, tool_search, MCP]
compatible_models: [text, tool_calling]
scope: current_conversation
status: active
published_at: '2026-10-09'
deprecation: null
content_hash: null
---

# 公司深度报告

## 适用与禁用场景
用于A股非金融公司深度报告组织、撰写与交付。禁止在写作流程重算盈利模型、
创作无依据的评级/目标价，或用篇幅要求填补事实缺口。

## 输入要求
公司身份、资料截止时间、同会话财务/事件/研报结构化结果及其原始来源。
外部结果须重新读取原文、登记来源，移除旧会话 ID；保留原有单位、期间、报表口径、公式与缺失原因。

## 主流程
1. 只在材料盘点时读取 [联动与证据规范](references/orchestration.md)，检查已有结果的对象、期间及状态。
2. 缺少分析时通过 `skill(name="financial-statement-analysis")`、`skill(name="company-event-monitor")`、
   `skill(name="research-report-digest")` 调用已启用技能；被禁用时列缺口，不修改启用配置。
3. 到预测步骤才调用 `skill(name="earnings-forecast")`，按其入口准备输入并执行计算，保留 computed.json。
   无可靠完整财年基期时跳过预测并列明缺口；不得从旧兼容脚本偷偷调用禁用能力。
4. 到写作步骤才读取 [报告模板](templates/report.md) 与 [输入契约](templates/input.schema.json)。
   按摘要、业务、行业、财务质量、事件、预测、风险、资料说明组织章节。
   逐项引用上游结果及其原文；用 [校验流程](references/quality.md) 核对数值、单位、期间及证据定位。
5. 调用 [报告组装校验](scripts/assemble_report.py) --input "<报告JSON>" --forecast "<预测结果JSON>"
   --analysis "<财务JSON>" --analysis "<事件JSON>" --analysis "<研报JSON>"
   --output "<会话输出目录>/deep/computed.json"；该脚本复制并校验上游预测，不重新计算。
   未获得预测时省略 --forecast，保留其他章节，结果为 partial。
6. 读取校验状态后执行 [导出](scripts/export_report.py) --input "<校验JSON>" --output-dir "<会话输出目录>/deep"。
   对话给摘要、限制及下载文件；不把工具成功等同事实核验。

## 完成标准
关键数字与上游 JSON 一致；每个事实段有原始定位，假设与解释可区分；
所有缺失章节、过期资料、未解决冲突显式列出。交付 JSON、Markdown、DOCX、XLSX。

## 关键约束与失败处理
引用必须真实可定位，不伪造 source_id/evidence_id。未知不写成零，母公司不混合合并口径。
预测只消费 earnings-forecast 结果；旧 scripts/forecast.py 保留给历史调用者，入口流程不使用。
数值或基期与上游冲突时拒绝组装，先回查来源；不修改结果使其匹配结论。
缺前置技能、无法读取原文或仅有摘要时交付 partial/blocked 和具体缺口。权限由已有工具执行。
