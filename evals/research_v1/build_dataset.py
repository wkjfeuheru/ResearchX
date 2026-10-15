"""Explicit corpus-authoring step. Normal evaluations only read frozen files.

python evals/research_v1/build_dataset.py --pdf-dir /tmp/researchx-evaluation-source-pdfs
"""

import argparse
import hashlib
import json
import logging
from decimal import Decimal as D
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from recipes import BROKERS, FORECAST_SKILL, REAL, SKILLS, SOURCES, SYNTHETIC

from researchx.evaluation.models import (
    Budget,
    EvalCase,
    Fault,
    PathRules,
    Requirement,
    SourceAsset,
    Turn,
)
from researchx.evaluation.calibration import calibration_cases

from typing import Literal
from typing_extensions import TypedDict
from researchx.evaluation.models import Category

Material = Literal["synthetic", "snapshot", "live"]
Difficulty = Literal["basic", "intermediate", "complex"]
ConflictKind = Literal["scope", "fact", "calculation", "interpretation"]
CONFLICT_KINDS: tuple[ConflictKind, ...] = ("scope", "fact", "calculation", "interpretation")


class SyntheticFixture(TypedDict, total=False):
    synthetic: bool
    case: str
    company: dict[str, str | bool]
    period: str
    currency: str | None
    unit: str
    scope: str
    revenue: str
    cost: str
    profit: str
    parent_profit: str
    prior_revenue: str
    assets: str
    liabilities: str
    equity: str
    begin_parent_equity: str
    end_parent_equity: str
    current_assets: str
    current_liabilities: str
    pretax_profit: str
    tax: str
    operating_cash: str
    investing_cash: str
    financing_cash: str
    fx_cash: str
    cash_increase: str
    published_at: str
    notes: list[str]
    events: list[dict[str, str | None]]
    reports: list[dict[str, str | None]]
    forecast: dict[str, str | int | None | dict[str, str]]
    declared_gaps: list[str]
    disagreement: dict[str, object]


ROOT = Path(__file__).resolve().parent


def source_asset(
    path: Path,
    identifier: str,
    url: str,
    title: str,
    family: str,
    provenance: Literal["synthetic", "official_snapshot", "live"],
    date: str,
    page: int | None = None,
) -> SourceAsset:
    return SourceAsset(
        id=identifier,
        path=str(path.relative_to(ROOT)),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        locator=url,
        title=title,
        family=family,
        provenance=provenance,
        published_at=date,
        page=page,
    )


def freeze_sources(pdf_dir: Path) -> dict[str, list[SourceAsset]]:
    result: dict[str, list[SourceAsset]] = {}
    catalog: dict[str, object] = {}
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    for name, info in SOURCES.items():
        raw = pdf_dir / (name + ".pdf")
        reader = PdfReader(raw)
        writer, chunks = PdfWriter(), []
        for i, page in enumerate(reader.pages[:24]):
            writer.add_page(page)
            chunks.append(f"\n## 原始PDF第{i + 1}页\n" + (page.extract_text() or ""))
        pdf = ROOT / "assets" / f"{name}-annual-first24.pdf"
        writer.write(pdf)
        text = pdf.with_suffix(".md")
        text.write_text(
            f"# {info['name']} {info['year']}年报前24页快照\n原始链接：{info['url']}\n"
            "部分页面不包含全部附注，未提供项不可编造。\n" + "\n".join(chunks),
            encoding="utf-8",
        )
        result[name] = [
            source_asset(
                p,
                name + ("-text" if p == text else "-pdf"),
                info["url"],
                info["name"] + f"年报快照（原PDF第1至24页；指标第{info['page']}页）",
                "official:" + name,
                "official_snapshot",
                info["date"],
                info["page"],
            )
            for p in (text, pdf)
        ]
        raw_text = "".join(chunks).replace(",", "").replace(" ", "").replace("\n", "")
        factor = D(1000) if name in {"catl", "sany", "midea"} else D(1)
        for key in ("revenue", "profit", "cash", "prior_revenue"):
            metric_value = info.get(key)
            if not isinstance(metric_value, str):
                raise ValueError(f"Source recipe has no numeric field: {key}")
            displayed = format(D(metric_value) / factor, "f")
            if displayed not in raw_text:
                raise ValueError(f"{name} {key} 未能在原文核对：{displayed}")
        catalog[name] = {
            **info,
            "raw_pdf_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            "snapshot_pages": list(range(1, min(24, len(reader.pages)) + 1)),
            "numeric_basis": "主要会计数据表，按原表头单位换算人民币元",
        }
    for name, (title, url, date, split) in BROKERS.items():
        raw = pdf_dir / (name + ".pdf")
        reader = PdfReader(raw)
        text = ROOT / "assets" / (name + ".md")
        text.write_text(
            f"# {title}\n原始链接：{url}\n"
            + "\n".join(
                f"\n## 原始PDF第{i + 1}页\n{p.extract_text() or ''}"
                for i, p in enumerate(reader.pages)
            ),
            encoding="utf-8",
        )
        result[name] = [
            source_asset(text, name, url, title, "broker:" + name, "official_snapshot", date, 1)
        ]
        catalog[name] = dict(
            title=title,
            url=url,
            date=date,
            split=split,
            raw_pdf_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
            note="机构原始研报的公开镜像；历史评级、预测不代表实际业绩或当前建议。",
        )
    (ROOT / "source_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    )
    return result


def synthetic(
    category: Category, i: int, identifier: str
) -> tuple[list[SourceAsset], dict[str, str], list[str], ConflictKind | None]:
    revenue, cost, profit = D(100 + i * 7), D(60 + i * 3), D(21 + i)
    data: SyntheticFixture = dict(
        synthetic=True,
        case=identifier,
        company=dict(
            name="样例精工", code="603999", market="SSE", industry="制造业", financial_sector=False
        ),
        period="2025",
        currency="CNY",
        unit="万元",
        scope="合并",
        revenue=str(revenue),
        cost=str(cost),
        profit=str(profit),
        parent_profit=str(profit - 1),
        prior_revenue="90",
        assets="200",
        liabilities="80",
        equity="120",
        begin_parent_equity="90",
        end_parent_equity="100",
        current_assets="100",
        current_liabilities="50",
        pretax_profit=str(profit + 7),
        tax="7",
        operating_cash="25",
        investing_cash="-10",
        financing_cash="-8",
        fx_cash="1",
        cash_increase="8",
        published_at="2026-10-01",
        notes=["合成材料不代表真实上市公司；合并净利润和归母净利润不可混用。"],
        events=[
            dict(date="2026-10-01", title="披露年度业绩", subject="样例精工", source="公司公告"),
            dict(date="2026-09-28", title="确定签订合同", subject="样例精工", source="公司公告"),
            dict(
                date="2026-09-01", title="计划扩产尚未投产", subject="样例精工", source="公司公告"
            ),
        ],
        reports=[
            dict(
                institution="样例机构甲（虚构）",
                date="2026-09-30",
                year="2027E",
                profit="28",
                unit="万元",
                currency="CNY",
                metric="归母净利润",
                target_price="12",
                assumption="需求增长10%",
                risk="竞争降价",
            ),
            dict(
                institution="样例机构乙（虚构）",
                date="2026-10-01",
                year="2027E",
                profit="30",
                unit="万元",
                currency="CNY",
                metric="归母净利润",
                assumption="需求增长15%",
                risk="原材料上涨",
            ),
        ],
        forecast=dict(
            base_year=2025,
            base_revenue=str(revenue),
            unit="万元",
            currency="CNY",
            growth="0.10",
            gross_margin="0.40",
            selling_rate="0.02",
            admin_rate="0.05",
            rd_rate="0.04",
            finance_rate="0.03",
            tax_surcharge_rate="0.01",
            other_operating_net="0",
            non_operating_net="0",
            tax_rate="0.25",
            parent_share="1",
            shares="1000000",
            scenario_growth={"悲观": "0.05", "基准": "0.10", "乐观": "0.15"},
            assumption_source="用户明确提供的分析者假设，不是实际业绩",
        ),
    )
    gaps = []
    if category == "financial":
        if i == 6:
            data["revenue"] = "0"
            gaps.append("零收入导致净利率不可计算")
        if i == 7:
            data["end_parent_equity"] = "-10"
            gaps.append("负权益时ROE解释不适用")
        if i == 8:
            data["currency"] = None
            gaps.append("币种未知")
        if i == 9:
            data["period"] = "2025H1"
            gaps.append("半年报不能自动年化")
    if category == "events":
        if i == 1:
            data["events"].append({**data["events"][0], "source": "媒体转述"})
        if i == 2:
            data["events"].append({**data["events"][1], "source": "转载站点"})
        if i == 3:
            data["events"][0]["date"] = "2026-09-30T17:30:00Z"
        if i == 4:
            data["events"][0]["date"] = None
            gaps.append("发布日期未知")
        if i in {5, 6, 7}:
            gaps.append({5: "计划不等于已实施", 6: "只有摘要缺原文", 7: "未连接消息推送"}[i])
        if i == 6:
            for event in data["events"]:
                event["source"] = "搜索摘要"
                event["verification"] = "未取得原始公告"
        if i == 10:
            data["notes"].append("股价同一基期从10元变为11元，笔记误称20%涨幅。")
        if i == 12:
            data["events"].append(
                dict(
                    date="2026-10-06T13:00:00+08:00",
                    title="截止后公告",
                    subject="样例精工",
                    source="公司公告",
                )
            )
        if i == 13:
            data["events"][0]["effective_date"] = "2026-11-01"
        if i in {14, 15}:
            data["events"][0]["subject"] = "控股母公司" if i == 14 else "未上市同名公司"
    if category == "digest":
        if i == 3:
            data["reports"][1]["date"] = None
            gaps.append("发布日期未知")
        if i == 4:
            data["reports"][1]["institution"] = None
            gaps.append("机构未知")
        if i == 6:
            data["reports"][1]["assumption"] = None
            gaps.append("成本假设缺失")
        if i == 7:
            data["reports"][1].pop("risk")
            gaps.append("风险章节缺失")
        if i == 8:
            data["reports"][1]["year"] = "2028E"
        if i == 9:
            data["reports"][1]["currency"] = "USD"
            gaps.append("汇率未提供")
        if i == 10:
            data["reports"][1].update(profit="0.30", unit="百万元")
        if i == 13:
            data["reports"][1]["profit"] = "25"
        if i == 14:
            data["reports"][1]["metric"] = "扣非归母净利润"
    if category == "deep":
        if i == 3:
            data["forecast"]["base_year"] = None
            data["period"] = "2025H1"
            gaps.append("完整财年基期未知")
        if i == 4:
            data["forecast"].pop("tax_rate")
            gaps.append("税率未给")
        if i == 5:
            data["forecast"].pop("shares")
            gaps.append("股本未给")
        if i in {6, 15}:
            gaps.append("行业原始资料缺失" if i == 6 else "无法预测完整三表")
    if category == "cross" and i in {4, 14}:
        gaps.append("缺少决定性原文")
    if gaps:
        data["declared_gaps"] = gaps
    has_conflict = (
        (category == "financial" and i >= 12)
        or (category == "events" and 8 <= i <= 11)
        or (category == "digest" and i >= 8)
        or (category == "deep" and 8 <= i <= 14)
        or (category == "cross" and (i < 8 or i in {10, 15}))
    )
    kind = CONFLICT_KINDS[i % 4] if has_conflict else None
    if category == "financial" and i == 15:
        kind = "calculation"
    if category == "digest" and i in {8, 9, 14}:
        kind = "scope"
    if category == "digest" and i in {11, 12}:
        kind = "interpretation"
    if category == "deep" and i in {12, 14}:
        kind = "fact"
    if category == "cross" and i == 10:
        kind = "interpretation"
    if has_conflict:
        assert kind is not None
        disagreements: dict[ConflictKind, list[dict[str, object]]] = {
            "scope": [
                dict(scope="合并", revenue=str(revenue)),
                dict(scope="母公司", revenue=str(revenue - 15)),
            ],
            "fact": [
                dict(version="正式修订", revenue=str(revenue)),
                dict(version="旧笔记", revenue=str(revenue - 10)),
            ],
            "calculation": [dict(original="1.2 billion 人民币元"), dict(note="误记为1.2亿元")],
            "interpretation": [
                dict(assumption="需求改善", view="条件性利润上升"),
                dict(assumption="竞争压价", view="条件性利润下降"),
            ],
        }
        sides = disagreements[kind]
        if category == "events":
            event_disagreements: dict[int, list[dict[str, object]]] = {
                8: [
                    dict(type="订单意向", amount="200万元"),
                    dict(type="正式合同", amount="120万元"),
                ],
                9: [
                    dict(version="旧媒体消息", statement="工厂已停产"),
                    dict(version="后续官方澄清", statement="仅计划检修，未停产"),
                ],
                10: [dict(original="股价10元到11元"), dict(note="媒体误记涨幅20%")],
                11: [
                    dict(view="扩产带来增长", assumption="需求足够"),
                    dict(view="扩产增加风险", assumption="需求不足"),
                ],
            }
            sides = event_disagreements.get(i, sides)
        if category == "digest":
            sides = [{"report": report} for report in data["reports"]]
            if i == 15:
                sides = [
                    dict(report="甲", statement="订单已取消"),
                    dict(report="乙", statement="订单未取消", limitation="均未提供原始公告"),
                ]
        if category == "deep":
            if i == 8:
                sides = [
                    dict(scope="集团", revenue=str(revenue)),
                    dict(scope="分部", revenue=str(revenue - 20)),
                ]
            if i == 9:
                sides = [
                    dict(period="2025E", revenue=str(revenue - 10)),
                    dict(period="2025A", revenue=str(revenue)),
                ]
            if i == 10:
                sides = [dict(note="漏扣管理费用"), dict(original=data["forecast"])]
            if i in {12, 14}:
                sides = [
                    dict(statement="乐观预测依赖订单"),
                    dict(statement="新版公告订单撤销", date="2026-10-04"),
                ]
        if category == "financial" and i == 15:
            sides = [
                dict(exact_yuan="189163654064.64"),
                dict(summary_yi_yuan="1891.64", rounding_places=2),
            ]
        data["disagreement"] = dict(
            kind=kind, sides=sides, core=not (category == "cross" and i == 5)
        )
    values = dict(
        gross_margin=str((revenue - cost) / revenue * 100),
        balance_residual="0",
        revenue_yoy=str((revenue / D(90) - 1) * 100),
        cash_profit=str(D(25) / profit),
        current_ratio="2",
        roe=str((profit - 1) / D(95) * 100),
        profit=str(profit),
        tax_residual="0",
        cash_residual="0",
        revenue=str(revenue),
        billion_yuan="1200000000",
        event_count=str(1 if category == "events" and i == 15 else 2),
        price_change="10",
        forecast_profit="28",
        forecast_gap="-3" if category == "digest" and i == 13 else "2",
        target_price="12",
        year1_revenue=str(revenue * D("1.1")),
        sensitivity=str(revenue * D("0.01")),
        year1_profit=str(revenue * D("1.1") * D("0.25") * D("0.75")),
    )
    path = ROOT / "assets" / (identifier + ".json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    asset = source_asset(
        path,
        identifier + "-source",
        f"https://fixtures.researchx.invalid/{identifier}",
        "样例精工合成资料",
        "synthetic:" + identifier,
        "synthetic",
        "2026-10-01",
    )
    return [asset], values, gaps, kind


def level(material: Material, i: int) -> Difficulty:
    basic, middle = {"synthetic": (6, 14), "snapshot": (4, 12), "live": (2, 6)}[material]
    return "basic" if i < basic else "intermediate" if i < middle else "complex"


def build(pdf_dir: Path) -> list[EvalCase]:
    (ROOT / "assets").mkdir(exist_ok=True)
    frozen = freeze_sources(pdf_dir)
    cases = []
    categories: tuple[Category, ...] = ("financial", "events", "digest", "deep", "cross")
    materials: tuple[tuple[Material, int], ...] = (("synthetic", 16), ("snapshot", 16), ("live", 8))
    for ci, category in enumerate(categories):
        for material, count in materials:
            for i in range(count):
                identifier = f"{category}-{'syn' if material == 'synthetic' else 'real' if material == 'snapshot' else 'live'}-{i + 1:02d}"
                split: Literal["dev", "holdout"] = (
                    "dev"
                    if i < (6 if ci < 3 else 5)
                    else "holdout"
                    if material == "live"
                    else "dev"
                )
                if material != "live":
                    split = "dev" if i < (12 if ci == 0 else 11) else "holdout"
                difficulty = level(material, i)
                assets: list[SourceAsset] = []
                urls: list[str] = []
                tags: list[str] = []
                faults: list[Fault] = []
                facts: list[str] = []
                numeric: Requirement | None = None
                kind: ConflictKind | None = None
                cutoff = "2026-10-06T12:00:00+08:00"
                if material == "synthetic":
                    title, goal, key = SYNTHETIC[category][i]
                    assets, values, gaps, kind = synthetic(category, i, identifier)
                    facts = [f"人工设计的合成任务，{goal}。", *gaps]
                    if gaps:
                        tags.append("missing_data")
                    if key:
                        unit = {
                            "gross_margin": "%",
                            "revenue_yoy": "%",
                            "roe": "%",
                            "cash_profit": "倍",
                            "current_ratio": "倍",
                            "billion_yuan": "元",
                            "event_count": "个",
                            "price_change": "%",
                            "target_price": "元/股",
                        }.get(key, "万元")
                        numeric = Requirement(
                            id="numeric",
                            description=goal,
                            check="numeric",
                            value=values[key],
                            unit=unit,
                            currency="CNY" if unit in {"万元", "元", "元/股"} else "",
                            period=(
                                "2025H1"
                                if category == "financial" and i == 9
                                else "2026"
                                if category == "events"
                                or key in {"year1_revenue", "year1_profit", "sensitivity"}
                                else "2027"
                                if category == "digest"
                                and key in {"forecast_profit", "forecast_gap"}
                                else "2026-09-30"
                                if key == "target_price"
                                else "2025"
                            ),
                            scope="合并"
                            if category == "financial" and key != "billion_yuan"
                            else "",
                            source_ids=[assets[0].id],
                        )
                        facts.append(
                            f"独立Decimal金标：{key}={values[key]} {unit}，不是模型生成答案。"
                        )
                else:
                    candidates = [name for name, s in SOURCES.items() if s["split"] == split]
                    name = (
                        "catl"
                        if category == "digest" and split == "dev"
                        else candidates[(i + ci) % len(candidates)]
                    )
                    source = SOURCES[name]
                    real_index = i if material == "snapshot" else i + 4
                    title = REAL[category][real_index]
                    goal = (
                        f"研究{source['name']}（{source['code']}）{source['year']}年资料：{title}。"
                    )
                    cutoff = f"{source['year'] + 1}-10-06T12:00:00+08:00"
                    if material == "snapshot":
                        assets = list(frozen[name])
                    else:
                        urls = [source["url"]]
                    facts = [
                        f"来源为{source['name']} {source['year']}年报，发布日{source['date']}；不是当前行情。",
                        f"主要会计数据定位原PDF第{source['page']}页；部分页面未包括全部附注。",
                    ]
                    if category == "digest":
                        brokers = (
                            ["catl-broker-a", "catl-broker-b"]
                            if split == "dev"
                            else [name + "-broker"]
                        )
                        for broker in brokers:
                            if material == "snapshot":
                                assets.extend(frozen[broker])
                            else:
                                urls.append(BROKERS[broker][1])
                        facts.append(
                            "机构预测/评级是发布时判断，不能写为已实现业绩或当前自主投资建议。"
                        )
                    if category == "financial" and real_index < 4:
                        value = (
                            D(source["revenue"]) / D(100000000),
                            D(source["profit"]),
                            (D(source["revenue"]) / D(source["prior_revenue"]) - 1) * 100,
                            D(source["cash"]) / D(source["profit"]),
                        )[real_index]
                        numeric = Requirement(
                            id="numeric",
                            description=title,
                            check="numeric",
                            value=str(value),
                            unit=("亿元", "元", "%", "倍")[real_index],
                            currency="CNY" if real_index < 2 else "",
                            period=str(source["year"]),
                            scope="合并",
                            atol="0.0001" if real_index == 0 else "0.01",
                            source_ids=[assets[0].id] if assets else [],
                            source_locators=[source["url"] + f"#page={source['page']}"],
                        )
                        facts.append(f"原文+独立换算金标：{value} {numeric.unit}。")
                    if category == "deep" and real_index in {0, 1, 2, 7}:
                        goal += "用户分析者假设：基准收入增速10%、悲观5%、乐观15%，连续三年不变；未给利润假设不填。"
                        value = D(source["revenue"]) * D("0.01" if real_index == 2 else "1.1")
                        numeric = Requirement(
                            id="numeric",
                            description="第一年收入或敏感性，人民币元",
                            check="numeric",
                            value=str(value),
                            unit="元",
                            currency="CNY",
                            period=str(source["year"] + 1),
                            scope="合并",
                            source_ids=[assets[0].id] if assets else [],
                            source_locators=[source["url"] + f"#page={source['page']}"],
                        )
                        facts.append(f"分析者设定的独立计算金标为{value}元，不是实际业绩。")
                    if material == "snapshot" and (
                        i in {4, 5, 6, 8, 14} or category == "deep" and i in {3, 13}
                    ):
                        tags.append("missing_data")
                    if material == "snapshot" and (
                        (category == "financial" and i in {12, 13})
                        or (category in {"events", "digest", "deep"} and 8 <= i <= 11)
                        or category == "cross"
                        and i < 8
                    ):
                        kind = CONFLICT_KINDS[i % 4]
                    if category == "cross" and material == "live" and i == 6:
                        kind = "interpretation"
                    if category == "cross" and material == "live" and i == 5:
                        assets = list(frozen[name])
                    if kind:
                        # Explicitly authored non-authoritative review notes are never called official facts.
                        note = ROOT / "assets" / (identifier + "-review-note.json")
                        claims = {
                            "scope": f"笔记混用{source['year']}年集团营业收入、营业总收入或其他期间收入。",
                            "fact": f"用户笔记称{source['year']}年营业收入为{D(source['revenue']) + D(10000000)}元，与正式表不同。",
                            "calculation": f"笔记把{source['revenue']}人民币元直接标成亿元，疑似单位换算错误。",
                            "interpretation": "同一经营结果被解释为确定改善或确定恶化，未说明需求和竞争假设。",
                        }
                        note.write_text(
                            json.dumps(
                                {
                                    "authored_evaluation_note": True,
                                    "case": identifier,
                                    "statement": claims[kind],
                                    "authority": "非权威用户审阅笔记；不是原始公告",
                                    "instruction": "与原始资料比较，不能将此笔记当作官方事实。",
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n"
                        )
                        assets.append(
                            source_asset(
                                note,
                                identifier + "-note",
                                "https://fixtures.researchx.invalid/" + identifier + "/note",
                                "人工构造的非权威审阅笔记",
                                "official:" + name,
                                "synthetic",
                                cutoff[:10],
                            )
                        )
                if category == "cross" and (
                    (material != "live" and i >= 8) or material == "live" and i >= 4
                ):
                    tags.append("recovery")
                    if material == "live" and i in {4, 5}:
                        faults = [
                            Fault(
                                tool="web_fetch",
                                kind="tool_error" if i == 4 else "timeout",
                                recovery="重新访问原始披露或明确转用提供快照",
                            )
                        ]
                    if material == "live" and i == 6:
                        faults = [
                            Fault(
                                tool="investigate_conflict",
                                kind="investigation_timeout",
                                recovery="核查超时后保留未解决争议",
                            )
                        ]
                    if i == 8 and material != "live":
                        faults = [
                            Fault(tool="read_file", kind="tool_error", recovery="重读有效原文")
                        ]
                    if i == 9:
                        faults = [
                            Fault(
                                tool="web_fetch",
                                kind="timeout",
                                recovery="改用提供的本地原文或保留缺口",
                            )
                        ]
                    if i == 10:
                        kind = "interpretation"
                        faults = [
                            Fault(
                                tool="investigate_conflict",
                                kind="investigation_timeout",
                                recovery="保留未解决争议",
                            )
                        ]
                conflict: Literal["none", "detect", "resolve", "unresolved", "reopen"] = (
                    "detect" if kind else "none"
                )
                if kind and material == "synthetic":
                    conflict = (
                        "unresolved"
                        if kind == "interpretation" or category == "cross" and i in {4, 10}
                        else "resolve"
                    )
                if category == "cross" and material == "live" and i == 6:
                    conflict = "unresolved"
                if kind:
                    tags.append("conflict")
                if kind and category in {"cross", "deep"} and i in {7, 14, 15}:
                    conflict = "reopen"
                skill = SKILLS.get(category)
                path = PathRules(
                    tool_groups=[["read_file", "web_fetch", "bash"]]
                    if material != "live"
                    else [["web_fetch", "web_search", "bash"]],
                    skills=[skill[0]] if skill else [],
                    scripts=[skill[1]] if skill else [],
                    forbidden_tools=["image_generate"],
                    forbidden_behaviors=["伪造实时数据", "把工具成功视为事实核验"],
                    plan_before_collection=difficulty != "basic",
                    verification_required=True,
                    conflict=conflict,
                    conflict_kind=kind,
                )
                if category == "cross" and material == "live" and i == 5:
                    path.tool_groups = [["read_file", "web_fetch", "web_search", "bash"]]
                disabled_skills = []
                if category == "cross":
                    skill_requirements: dict[int, tuple[Category, ...]] = {
                        0: ("financial", "digest"),
                        1: ("financial", "events"),
                        2: ("financial", "digest"),
                        3: ("deep", "digest"),
                        4: ("deep", "events"),
                        5: ("financial", "digest"),
                        6: ("financial", "deep"),
                        7: ("financial", "digest"),
                        8: ("financial",),
                        9: ("financial",),
                        10: ("deep", "digest"),
                        11: ("financial",),
                        12: ("financial",),
                        13: ("financial",),
                        15: ("financial", "events"),
                    }
                    needs = skill_requirements.get(i, ())
                    path.skills = [SKILLS[k][0] for k in needs]
                    path.scripts = [SKILLS[k][1] for k in needs]
                    if i == 14:
                        disabled_skills = [
                            "financial-statement-analysis",
                            "company-event-monitor",
                            "research-report-digest",
                            "earnings-forecast",
                            "deep-investment-report",
                            "financial-commentary",
                            "industry-commentary",
                            "industry-deep-dive",
                        ]
                if "deep-investment-report" in path.skills:
                    path.skills.append(FORECAST_SKILL[0])
                    path.scripts.append(FORECAST_SKILL[1])
                requirements = [
                    Requirement(id="delivery", description=goal),
                    Requirement(
                        id="provenance", description="结论绑定原文，注明期间、口径和未知条件"
                    ),
                ]
                if numeric:
                    requirements.append(numeric)
                if material == "synthetic" and category == "financial" and i == 0:
                    raw = json.loads((ROOT / assets[0].path).read_text())
                    requirements.append(
                        Requirement(
                            id="net_margin",
                            description="净利率，不能混用归母和合并净利润",
                            check="numeric",
                            value=str(D(raw["profit"]) / D(raw["revenue"]) * 100),
                            unit="%",
                            period="2025",
                            scope="合并",
                            rtol="0",
                            source_ids=[assets[0].id],
                        )
                    )
                if "missing_data" in tags:
                    requirements.append(
                        Requirement(
                            id="limitations",
                            check="limitation",
                            description="缺失项、不可计算项或材料覆盖限制明确说明，不擅自补数",
                        )
                    )
                if kind:
                    requirements.append(
                        Requirement(
                            id="conflict",
                            description="核对双方资料和口径，登记实际争议，保留条件或裁决依据",
                        )
                    )
                if skill and (
                    (material == "synthetic" and i in {7, 15}) or material == "snapshot" and i == 15
                ):
                    requirements.extend(
                        Requirement(
                            id="export_" + ext,
                            check="artifact",
                            description=f"导出可读取的{ext}文件",
                            value="." + ext,
                        )
                        for ext in ("md", "json", "docx", "xlsx")
                    )
                prompt = goal + "\n实际调用适用技能并执行必要计算，给出有来源结果，不只介绍方法。"
                if material == "live":
                    prompt += " 本任务必须实际访问公开原文并记录当次可用性；检索失败与未找到资料分开报告，不仅凭已有知识回答。"
                if kind:
                    prompt += " 对资料或审阅笔记中的争议使用研究冲突流程显式登记，评估双方原文和口径；不要隐藏争议。"
                for fault in faults:
                    if fault.tool == "web_fetch" and assets:
                        prompt += f" 先用web_fetch读取 {assets[0].locator}，读取失败再改用附件。"
                    elif fault.tool == "investigate_conflict":
                        prompt += " 登记核心解释争议后调用investigate_conflict；核查未完成时保留未解决状态。"
                if numeric:
                    prompt += f" 数值统一用{numeric.unit}，期间{numeric.period}，口径{numeric.scope or '按原文'}。"
                turns = [Turn(prompt=prompt)]
                if category == "cross" and material != "live" and i in {11, 12, 13, 15}:
                    actions: dict[int, Literal["cancel_resume", "steer", "restart", "submit"]] = {
                        11: "cancel_resume",
                        12: "steer",
                        13: "restart",
                        15: "submit",
                    }
                    action = actions[i]
                    follow = (
                        "范围改为现金流质量：先更新目标和计划，只分析现金流与归母利润。"
                        if i == 12
                        else "继续核验已提供原文，重新检查来源版本，再回答。"
                    )
                    turns.append(
                        Turn(
                            prompt=follow,
                            action=action,
                            trigger="first_collection" if i in {11, 12} else "after_turn",
                        )
                    )
                if category == "cross" and material == "live" and i >= 4:
                    turns.append(
                        Turn(
                            prompt="继续核对原始披露；访问失败说明限制，错误响应不是证据。",
                            action="cancel_resume" if i == 7 else "submit",
                            trigger="first_collection" if i == 7 else "after_turn",
                        )
                    )
                family = (
                    "synthetic:" + identifier if material == "synthetic" else "official:" + name
                )
                cases.append(
                    EvalCase(
                        id=identifier,
                        category=category,
                        family=family,
                        split=split,
                        difficulty=difficulty,
                        environment="live" if material == "live" else "fixed",
                        material=material,
                        title=title,
                        turns=turns,
                        cutoff=cutoff,
                        assets=assets,
                        live_urls=urls,
                        disabled_skills=disabled_skills,
                        requirements=requirements,
                        reference_facts=facts,
                        path=path,
                        budget=Budget(
                            timeout_seconds={"basic": 240, "intermediate": 600, "complex": 900}[
                                difficulty
                            ],
                            model_calls={"basic": 40, "intermediate": 64, "complex": 96}[
                                difficulty
                            ],
                            total_tokens={
                                "basic": 3000000,
                                "intermediate": 10000000,
                                "complex": 24000000,
                            }[difficulty],
                        ),
                        faults=faults,
                        tags=list(dict.fromkeys(tags)),
                        annotation_basis="合成原始输入和独立Decimal公式"
                        if material == "synthetic"
                        else "source_catalog.json 原始文档、页码、原始/预测口径及独立数值换算",
                        review_status="rule_checked"
                        if material == "synthetic"
                        else "source_checked",
                    )
                )
    # Revision tasks actually reveal new material in a later user turn.
    for case in cases:
        if case.category == "deep":
            index = int(case.id.rsplit("-", 1)[1])
            synthetic_forecast = case.material == "synthetic" and index in {1, 2, 8}
            real_forecast = case.material != "synthetic" and any(
                phrase in case.title for phrase in ("三年收入回测", "三情景收入", "八章节")
            )
            if synthetic_forecast or real_forecast:
                base = next(r for r in case.requirements if r.id == "numeric")
                if synthetic_forecast:
                    inputs = json.loads((ROOT / case.assets[0].path).read_text())["forecast"]
                    revenue = D(inputs["base_revenue"])
                    year = int(inputs["base_year"])
                else:
                    if base.value is None:
                        raise ValueError("Forecast numeric requirement has no value")
                    revenue = D(base.value) / D("1.1")
                    year = int(base.period) - 1
                scenarios = (
                    {"基准": D("0.10")}
                    if index == 1
                    else {"悲观": D("0.05"), "基准": D("0.10"), "乐观": D("0.15")}
                )
                years = [1] if synthetic_forecast and index == 2 else [1, 2, 3]
                for scenario, growth in scenarios.items():
                    for offset in years:
                        forecast_revenue = revenue * (1 + growth) ** offset
                        forecast_values = [("revenue", "收入", forecast_revenue)]
                        if synthetic_forecast and index in {1, 8}:
                            forecast_values.append(
                                ("profit", "合并净利润", forecast_revenue * D("0.25") * D("0.75"))
                            )
                        for metric, label, value in forecast_values:
                            case.requirements.append(
                                Requirement(
                                    id=f"forecast_{scenario}_{offset}_{metric}",
                                    description=f"{scenario}情景{year + offset}年{label}",
                                    check="numeric",
                                    value=str(value),
                                    unit=base.unit,
                                    currency="CNY",
                                    period=str(year + offset),
                                    scope="合并",
                                    rtol="0",
                                    source_ids=base.source_ids,
                                    source_locators=base.source_locators,
                                )
                            )
                case.reference_facts.append(
                    "各年独立按基期收入×(1+情景增速)^年数计算；模拟合并净利按收入×25%税前率×75%税后系数计算。"
                )
        for requirement in case.requirements:
            if requirement.check == "numeric":
                requirement.rtol = "0"
        if case.path.conflict != "reopen":
            continue
        late_path = ROOT / "assets" / (case.id + "-revision.json")
        replacement = None
        if case.material == "synthetic":
            original = case.assets[0]
            initial = json.loads((ROOT / original.path).read_text())
            if case.id == "deep-syn-15":
                initial["disagreement"] = [
                    {"statement": "乐观预测依赖订单继续有效"},
                    {"statement": "审阅笔记认为订单存在取消风险，原始撤销资料尚未收到"},
                ]
                (ROOT / original.path).write_text(
                    json.dumps(initial, ensure_ascii=False, indent=2) + "\n"
                )
                original.sha256 = hashlib.sha256((ROOT / original.path).read_bytes()).hexdigest()
            revised = dict(initial)
            revised["revision"] = {
                "version": 2,
                "published_at": "2026-10-05",
                "statement": "新增合成原始披露：关键订单已经撤销，旧版需求改善假设须重新核查。",
                "origin": "虚构公司的评测原始数据，不是真实公告",
            }
            if case.id == "cross-syn-16":
                revised["revenue"] = str(D(initial["revenue"]) + D("5"))
                revised["revision"]["statement"] = (
                    "合成原始报表更正：合并收入增加5万元；原值被替代，其余输入未变。"
                )
                for r in case.requirements:
                    if r.id == "numeric":
                        r.value = revised["revenue"]
                case.reference_facts.append(
                    "更正版收入为" + revised["revenue"] + "万元，原收入已失效。"
                )
            late_path.write_text(json.dumps(revised, ensure_ascii=False, indent=2) + "\n")
            replacement = original.id
        else:
            late_path.write_text(
                json.dumps(
                    {
                        "authored_evaluation_note": True,
                        "statement": "审阅人更正：撤回此前笔记中的确定性判断；原始年报不变。此前基于笔记的解释应重新核查，不能声称这是公司新公告。",
                        "authority": "用户修订的非权威审阅笔记",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        late = source_asset(
            late_path,
            case.id + "-revision",
            "https://fixtures.researchx.invalid/" + case.id + "/revision",
            "第二轮新增的更正材料",
            case.family,
            "synthetic",
            "2026-10-05",
        )
        late.available_from_turn = 1
        late.replaces_asset_id = replacement
        case.assets.append(late)
        if case.id == "cross-syn-16":
            next(r for r in case.requirements if r.id == "numeric").source_ids = [late.id]
        case.turns[0].prompt += " 本轮先核查初始资料；下一轮才提供更正材料。"
        follow = (
            "将本轮资料截止更新为2026-10-06T12:00:00+08:00。新增更正材料 materials/"
            + late_path.name
            + "。请实际读取新资料，修订或撤回旧证据，重新打开受影响的冲突并核查依赖结论；"
            "最终回答以新版本为准，说明哪些判断发生变化和仍然未知的条件。"
        )
        case.turns = [case.turns[0], Turn(prompt=follow)]
        case.requirements.append(
            Requirement(
                id="revision",
                description="识别后来提供的修订来源，更新依赖结论和原裁决，不继续引用失效证据",
                source_ids=[late.id],
            )
        )
        case.reference_facts.append("更正材料仅第二轮可见，不能将旧核验状态直接赋予新来源。")
        case.tags = list(dict.fromkeys([*case.tags, "recovery", "evidence_revision"]))
    # Bind every gold statement and requirement to an explicit source location.
    # These annotations are scorer-only; agent_input deliberately excludes them.
    for case in cases:
        locations = [
            a.locator + (f"#page={a.page}" if a.page else "") for a in case.assets
        ] or list(case.live_urls)
        case.reference_locations = {
            fact: list(dict.fromkeys(locations)) for fact in case.reference_facts
        }
        if case.material == "synthetic":
            primary = case.assets[0]
            data = json.loads((ROOT / primary.path).read_text())
            keys = {
                "financial": (
                    "period",
                    "currency",
                    "scope",
                    "revenue",
                    "cost",
                    "profit",
                    "parent_profit",
                    "operating_cash",
                    "assets",
                    "liabilities",
                    "equity",
                ),
                "events": ("events", "declared_gaps"),
                "digest": ("reports", "declared_gaps"),
                "deep": ("period", "forecast", "declared_gaps"),
                "cross": (
                    "period",
                    "currency",
                    "revenue",
                    "parent_profit",
                    "operating_cash",
                    "declared_gaps",
                ),
            }[case.category]
            for key in keys:
                if key in data:
                    fact = f"初始合成资料 /{key}：" + json.dumps(data[key], ensure_ascii=False)
                    case.reference_facts.append(fact)
                    case.reference_locations[fact] = [primary.locator + "#/" + key]
        else:
            company = case.family.removeprefix("official:")
            source = SOURCES[company]
            location = source["url"] + f"#page={source['page']}"
            for key, label in (
                ("revenue", "营业收入"),
                ("profit", "归母净利润"),
                ("cash", "经营活动现金流量净额"),
                ("prior_revenue", "上年营业收入"),
            ):
                year = source["year"] - 1 if key == "prior_revenue" else source["year"]
                fact = f"{source['name']} {year}年{label}为{source.get(key)}人民币元，原表头单位已换算。"
                case.reference_facts.append(fact)
                case.reference_locations[fact] = [location]
        for requirement in case.requirements:
            if not requirement.source_ids and case.assets:
                requirement.source_ids = [a.id for a in case.assets]
            if not requirement.source_locators:
                referenced = [a for a in case.assets if a.id in requirement.source_ids]
                requirement.source_locators = list(
                    dict.fromkeys(
                        [a.locator + (f"#page={a.page}" if a.page else "") for a in referenced]
                        or locations
                    )
                )
    (ROOT / "cases.jsonl").write_text(
        "\n".join(c.model_dump_json() for c in cases) + "\n", encoding="utf-8"
    )
    calibration: list[dict[str, object]] = []
    for c in calibration_cases(cases):
        calibration.extend(
            dict(
                case_id=c.id,
                category=c.category,
                difficulty=c.difficulty,
                material=c.material,
                reviewer=None,
                human_scores=None,
                disagreement_notes=None,
                status="pending",
            )
            for _ in range(1)
        )
    (ROOT / "calibration.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in calibration) + "\n"
    )
    return cases


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    print(f"Built {len(build(parser.parse_args().pdf_dir))} complete cases")
