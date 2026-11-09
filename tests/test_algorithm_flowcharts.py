"""输入边界和静态SVG回归，不把基础限制冒称完整Mermaid语法检查。"""

import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from axiomforge.algorithm_flowcharts import (
    MAX_SOURCE_BYTES,
    MAX_SVG_BYTES,
    FlowchartDraft,
    FlowchartError,
    content_hash,
    sanitize_svg,
    seed_draft,
    source_hash,
    validate_draft,
    validate_source,
)


def test_seed_preserves_user_original_and_configurable_scope():
    seed = seed_draft()
    # 固定用户原文指纹，任务目录归档后测试仍可验证种子未改写。
    assert (
        source_hash(seed.source)
        == "292c17578defc3080cf4844066c507f2533e99313b372d34d56a83677f3eb2b0"
    )
    validate_draft(seed)
    assert seed.source.count("-->") == 9
    assert "class T,C configurable;" in seed.source
    assert "待关联" in seed.annotations["T"]["description"]
    assert "待关联" in seed.annotations["C"]["description"]
    assert "所有AHU" in seed.annotations["N"]["description"]
    assert "对应AHU" in seed.annotations["B"]["description"]
    assert "全局关闭" not in seed.annotations["B"]["description"].split("，")[0]


@pytest.mark.parametrize(
    "source",
    [
        "flowchart TD\nA[中文] -->|是| B{是否运行？}",
        "%% 普通注释\ngraph LR; A --> B",
        "flowchart\n subgraph zone\n A --> B\n end",
        "flowchart TD\nA --> B\nclassDef warm fill:rgb(255, 240, 204),stroke:#D97706;",
        "flowchart TD\nA[回风温度 < 设定温度] --> B",
    ],
)
def test_basic_flowcharts_and_restricted_classdef_are_allowed(source):
    validate_source(source)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "pie\n A: 2",
        "sequenceDiagram\n A->>B: text",
        "---\nconfig:\n  securityLevel: loose\n---\nflowchart TD\nA-->B",
        "%%{init: {'securityLevel': 'loose'}}%%\nflowchart TD\nA-->B",
        'flowchart TD\nA["<script>alert(1)</script>"]',
        'flowchart TD\nA["&lt;img src=x onerror=alert(1)&gt;"]',
        'flowchart TD\nA --> B; click A "https://example.com"',
        "flowchart TD\nclick A callback",
        'flowchart TD\nA@{img: "x"}',
        "flowchart TD\nclassDef bad fill:url(//example.com/x);",
        "flowchart TD\nclassDef bad font-family:arbitrary;",
        "flowchart TD\nclassDef bad stroke-width:expression(foo);",
        "flowchart TD\nclassDef bad position:fixed;",
        "flowchart TD\nstyle A fill:red;",
        "flowchart TD\nlinkStyle 0 stroke:red;",
        'flowchart TD\nA["javascript:alert(1)"]',
        'flowchart TD\nA["data:image/svg+xml;base64,abc"]',
        'flowchart TD\nA["x\x00y"]',
    ],
)
def test_unsupported_types_and_active_source_are_rejected(source):
    with pytest.raises(FlowchartError):
        validate_source(source)


def test_source_limit_counts_utf8_bytes_not_characters():
    source = "flowchart TD\n%% " + "中" * (MAX_SOURCE_BYTES // 3)
    assert len(source) < MAX_SOURCE_BYTES
    with pytest.raises(FlowchartError, match="64KiB"):
        validate_source(source)


def test_server_validation_is_not_full_syntax_validation():
    # 残缺括号由浏览器Mermaid解析器报告；服务器仅检查类型和支持能力。
    validate_source("flowchart TD\nA[残缺节点")


def test_preview_hash_is_source_exact_and_content_hash_includes_metadata():
    draft = FlowchartDraft("图", "说明", "flowchart TD\nA-->B", {"A": {"description": "甲"}})
    assert source_hash(draft.source) != source_hash(draft.source + "\n")
    assert content_hash(draft) != content_hash(replace(draft, name="新图"))
    assert content_hash(draft) != content_hash(
        replace(draft, annotations={"A": {"description": "乙"}})
    )
    assert content_hash(draft) == content_hash(
        replace(draft, annotations={"A": {"description": "甲"}})
    )


@pytest.mark.parametrize(
    "draft",
    [
        FlowchartDraft(" ", "", "flowchart TD"),
        FlowchartDraft("名" * 101, "", "flowchart TD"),
        FlowchartDraft("图", "说" * 2001, "flowchart TD"),
        FlowchartDraft("图", "", "flowchart TD", {"T": {"value": 26}}),
        FlowchartDraft("图", "", "flowchart TD", {"": {"description": "说明"}}),
    ],
)
def test_metadata_and_annotations_are_bounded(draft):
    with pytest.raises(FlowchartError):
        validate_draft(draft)


def test_static_mermaid_svg_retains_text_markers_style_and_internal_use():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" id="diagram1" viewBox="0 0 500 800">
      <style>#diagram1 .node rect{fill:#FFF0CC;stroke:#D97706;stroke-width:3px}
      #diagram1 .edge{marker-end:url(#arrow)}</style>
      <title>AHU运行策略 · 图示版本1</title><desc>中文说明</desc>
      <defs><marker id="arrow"><path d="M 0 0 L 10 10"/></marker></defs>
      <g class="node configurable"><rect width="100" height="60"/>
      <text><tspan>夜间条件？</tspan></text></g>
      <use href="#arrow"/><path marker-end="url('#arrow')" d="M0,0L10,10"/>
      </svg>"""
    clean = sanitize_svg(svg)
    root = ET.fromstring(clean)
    assert root.get("viewBox") == "0 0 500 800"
    assert "夜间条件？" in clean
    assert "#FFF0CC" in clean
    assert "url(#arrow)" in clean
    assert "marker" in clean
    assert sanitize_svg(clean) == clean


def test_mermaid_default_animation_css_is_removed_without_rejecting_static_graph():
    svg = """<svg xmlns="http://www.w3.org/2000/svg"><style>
      .edge-animation-fast{stroke-width:2;animation:dash 20s linear infinite;fill:none}
      @keyframes dash {to{stroke-dashoffset:0}}
      .node rect{fill:#fff;transition:fill 1s}
      </style><rect style="animation: dash 1s;fill:red"/></svg>"""
    clean = sanitize_svg(svg)
    assert "animation" not in clean.split("{", 1)[-1].split("}", 1)[0]
    assert "@keyframes" not in clean and "transition:" not in clean
    assert "fill:red" in clean and "fill:#fff" in clean


def test_standard_static_filters_and_gradients_are_allowed():
    svg = """<svg xmlns="http://www.w3.org/2000/svg"><defs>
      <filter id="shadow"><feDropShadow dx="1" dy="1" stdDeviation="2"/>
      <feGaussianBlur stdDeviation="2"/><feOffset dx="1" dy="1"/>
      <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <linearGradient id="warm"><stop offset="0%" stop-color="#fff"/></linearGradient>
      </defs><rect filter="url(#shadow)" fill="url(#warm)"/></svg>"""
    clean = sanitize_svg(svg)
    assert "feDropShadow" in clean and "feGaussianBlur" in clean and "url(#shadow)" in clean


@pytest.mark.parametrize("property_name", ["animation", "transition", "-webkit-animation"])
def test_inline_animation_without_trailing_semicolon_is_removed(property_name):
    # 行内CSS允许最后一个声明省略分号，也必须导出为静态图示。
    svg = f'<svg><rect style="fill:red;{property_name}:dash 1s"/></svg>'
    clean = sanitize_svg(svg)
    assert property_name not in clean
    assert "fill:red" in clean


@pytest.mark.parametrize(
    "body",
    [
        "<script>alert(1)</script>",
        "<foreignObject><div>html</div></foreignObject>",
        '<rect onload="alert(1)"/>',
        '<g ONCLICK="alert(1)"/>',
        '<image href="https://example.com/a.png"/>',
        '<use href="//example.com/a.svg#x"/>',
        '<a href="#node"><text>link</text></a>',
        '<animate attributeName="x"/>',
        '<set attributeName="fill" to="red"/>',
        '<animateTransform attributeName="transform"/>',
        '<rect style="fill:url(https://example.com/x)"/>',
        '<rect fill="url(//example.com/x)"/>',
        '<rect style="fill:expres/**/sion(alert(1))"/>',
        '<style>@import "//example.com/x";</style>',
        "<style>.node{fill:u\\72l(//example.com/x)}</style>",
        '<use href="javascript:alert(1)"/>',
        '<g xml:base="https://example.com/"><use href="#x"/></g>',
    ],
)
def test_active_svg_is_rejected(body):
    with pytest.raises(FlowchartError):
        sanitize_svg(f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>')


@pytest.mark.parametrize(
    "svg",
    [
        '<!DOCTYPE svg [<!ENTITY x "active">]><svg>&x;</svg>',
        '<?xml-stylesheet href="https://example.com/x"?><svg/>',
        "<html>not svg</html>",
        "<svg><g></svg>",
        "<svg>" + "<g>" * 66 + "</g>" * 66 + "</svg>",
    ],
)
def test_invalid_svg_and_entities_are_rejected(svg):
    with pytest.raises(FlowchartError):
        sanitize_svg(svg)


def test_svg_size_limit():
    with pytest.raises(FlowchartError, match="2MiB"):
        sanitize_svg("<svg><text>" + "x" * MAX_SVG_BYTES + "</text></svg>")
