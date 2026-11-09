"""流程图的文本、版本与SVG边界；图示不代表可执行算法或现场参数。"""

from __future__ import annotations

import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files

from axiomforge.demo import PROJECTS

MAX_SOURCE_BYTES = 64 * 1024
MAX_SVG_BYTES = 2 * 1024 * 1024
PROJECT_IDS = frozenset(project.id for project in PROJECTS)
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class FlowchartError(ValueError):
    """供界面显示的脱敏错误，不携带数据库路径、SQL或原始图示内容。"""


class FlowchartConflictError(FlowchartError):
    """保存基线已过期，调用方必须保留草稿并请用户重新载入。"""


@dataclass(frozen=True)
class FlowchartDraft:
    name: str
    description: str
    source: str
    annotations: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowchartRevision:
    flowchart_id: str
    project_id: str
    revision: int
    name: str
    description: str
    source: str
    annotations: dict[str, dict[str, str]]
    content_hash: str
    created_at: datetime
    derived_from_revision: int | None = None


def validate_project(project_id: str) -> None:
    if not isinstance(project_id, str) or project_id not in PROJECT_IDS:
        raise FlowchartError("请选择有效的工作台项目。")


def source_hash(source: str) -> str:
    """预览只对完全相同的源码有效，空白与注释变化也使旧预览过期。"""
    if not isinstance(source, str):
        raise FlowchartError("Mermaid源码必须是文本。")
    try:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise FlowchartError("Mermaid源码包含无法编码的字符。") from None


_COLORS = re.compile(r"(?:#[\da-f]{3,8}|[a-z]+|(?:rgb|rgba|hsl|hsla)\([\d\s.,%+-]+\))", re.I)
_STYLE_VALUES = {
    "fill": _COLORS,
    "stroke": _COLORS,
    "color": _COLORS,
    "stroke-width": re.compile(r"\d+(?:\.\d+)?(?:px|pt)?"),
    "stroke-dasharray": re.compile(r"\d+(?:[\s ]+\d+)*"),
    "font-size": re.compile(r"\d+(?:\.\d+)?(?:px|pt|em|rem|%)?"),
    "font-weight": re.compile(r"normal|bold|[1-9]00"),
    "font-style": re.compile(r"normal|italic|oblique"),
    "text-align": re.compile(r"left|center|right"),
}


def validate_source(source: str) -> None:
    """只做基础格式和能力限制；完整Mermaid语法仍须由浏览器引擎解析。"""
    source_hash(source)
    if not source.strip() or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise FlowchartError("Mermaid源码不能为空，且不能超过64KiB。")
    decoded = html.unescape(source)
    if "\x00" in decoded or re.search(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", decoded):
        raise FlowchartError("Mermaid源码包含不支持的控制字符。")
    if re.search(r"%%\s*\{|^\s*---", decoded, re.M):
        raise FlowchartError("不支持Mermaid配置指令或配置前言。")
    if re.search(r"<\s*/?\s*[a-z!][^>]*>|<\?", decoded, re.I):
        raise FlowchartError("流程图不支持HTML、脚本或外部内容。")
    if re.search(r"(?:https?|javascript|vbscript|data|file|ftp)\s*:|@\s*\{", decoded, re.I):
        raise FlowchartError("流程图不支持链接、图片或外部资源。")
    if re.search(r"(?:^|;)\s*(?:click|link|style|linkStyle)\b", decoded, re.I | re.M):
        raise FlowchartError("流程图不支持click、链接或自由样式指令，请使用受限classDef。")
    if re.search(r"url\s*\(|expression\s*\(|@import|\\", decoded, re.I):
        raise FlowchartError("流程图样式包含不支持的资源或转义。")
    lines = [
        line.strip()
        for line in source.lstrip("\ufeff").splitlines()
        if line.strip() and not line.lstrip().startswith("%%")
    ]
    if not lines or not re.match(
        r"^(?:flowchart|graph)(?:[ \t]+(?:TB|TD|BT|RL|LR))?[ \t]*(?:;|$)", lines[0]
    ):
        raise FlowchartError("首版仅支持flowchart或graph流程图。")
    directives = re.findall(r"(?:^|;)\s*classDef\b([^;\r\n]*)", source, re.I | re.M)
    for directive in directives:
        match = re.fullmatch(r"\s+[\w-]+(?:\s*,\s*[\w-]+)*\s+(.+)", directive)
        if not match:
            raise FlowchartError("classDef格式不正确。")
        # 颜色函数可包含逗号，只有括号之外的逗号才分隔属性。
        properties = re.split(r",(?![^()]*\))", match[1])
        for item in properties:
            key, separator, value = item.strip().partition(":")
            validator = _STYLE_VALUES.get(key.lower())
            if not separator or validator is None or not validator.fullmatch(value.strip()):
                raise FlowchartError("classDef只支持节点颜色、边框和基础文字样式。")


def validate_draft(draft: FlowchartDraft) -> None:
    if not isinstance(draft, FlowchartDraft):
        raise FlowchartError("流程图草稿结构不正确。")
    if not isinstance(draft.name, str) or not draft.name.strip() or len(draft.name) > 100:
        raise FlowchartError("图示名称不能为空，且不能超过100字。")
    if not isinstance(draft.description, str) or len(draft.description) > 2000:
        raise FlowchartError("图示说明不能超过2000字。")
    validate_source(draft.source)
    annotations = draft.annotations
    if not isinstance(annotations, dict) or len(annotations) > 500:
        raise FlowchartError("节点注释结构不正确或数量过多。")
    for node_id, annotation in annotations.items():
        if not isinstance(node_id, str) or not 1 <= len(node_id) <= 128:
            raise FlowchartError("节点注释ID不正确。")
        if not isinstance(annotation, dict) or len(annotation) > 20:
            raise FlowchartError("节点注释结构不正确。")
        if any(
            not isinstance(key, str)
            or not 1 <= len(key) <= 80
            or not isinstance(value, str)
            or len(value) > 4000
            for key, value in annotation.items()
        ):
            raise FlowchartError("节点注释的字段或文字超出限制。")
    try:
        if len(json.dumps(annotations, ensure_ascii=False).encode()) > MAX_SOURCE_BYTES:
            raise FlowchartError("节点注释总量不能超过64KiB。")
        draft.name.encode("utf-8")
        draft.description.encode("utf-8")
    except (TypeError, UnicodeError):
        raise FlowchartError("图示说明或节点注释包含无法编码的字符。") from None


def content_hash(draft: FlowchartDraft) -> str:
    validate_draft(draft)
    payload = json.dumps(
        {
            "name": draft.name,
            "description": draft.description,
            "source": draft.source,
            "annotations": draft.annotations,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seed_draft() -> FlowchartDraft:
    """种子逻辑是用户原文；参数仅作说明，不虚构现场绑定。"""
    annotations = {
        "T": {
            "title": "夜间时间条件",
            "type": "判断",
            "configurable": "true",
            "description": "可配置条件：夜间时间范围待关联，未绑定真实时间段。",
        },
        "C": {
            "title": "回风温度条件",
            "type": "判断",
            "configurable": "true",
            "description": "可配置条件：温度设定值待关联，未绑定真实参数。",
        },
        "N": {
            "title": "全局关闭",
            "type": "动作",
            "description": "作用于所有AHU及新风阀，尚未绑定具体设备清单。",
        },
        "B": {
            "title": "对应设备关闭",
            "type": "动作",
            "description": "仅作用于对应AHU及新风阀，不等同于全局关闭。",
        },
    }
    return FlowchartDraft(
        "AHU运行策略",
        "夜间、人员／航班保障与预冷、回风温度及冷水机组条件下的AHU／新风阀决策说明",
        files("axiomforge").joinpath("resources/hangxin-ahu.mmd").read_text(encoding="utf-8"),
        annotations,
    )


_SVG_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "marker",
        "path",
        "rect",
        "polygon",
        "polyline",
        "line",
        "circle",
        "ellipse",
        "text",
        "tspan",
        "title",
        "desc",
        "style",
        "clipPath",
        "linearGradient",
        "radialGradient",
        "stop",
        "pattern",
        "mask",
        "use",
        "metadata",
        "filter",
        "feBlend",
        "feColorMatrix",
        "feComponentTransfer",
        "feComposite",
        "feConvolveMatrix",
        "feDiffuseLighting",
        "feDisplacementMap",
        "feDistantLight",
        "feDropShadow",
        "feFlood",
        "feFuncA",
        "feFuncB",
        "feFuncG",
        "feFuncR",
        "feGaussianBlur",
        "feMerge",
        "feMergeNode",
        "feMorphology",
        "feOffset",
        "fePointLight",
        "feSpecularLighting",
        "feSpotLight",
        "feTile",
        "feTurbulence",
    }
)
_FRAGMENT = re.compile(r"#[A-Za-z_][\w.:-]*")


def _safe_urls(value: str) -> None:
    if re.search(
        r"(?:https?|javascript|vbscript|data|file|ftp)\s*:|expression\s*\(|\\", value, re.I
    ):
        raise FlowchartError("SVG包含外部资源或活动样式。")
    for match in re.finditer(r"url\s*\((.*?)\)", value, re.I | re.S):
        target = match[1].strip().strip("\"'")
        if not _FRAGMENT.fullmatch(target):
            raise FlowchartError("SVG只能引用图内资源。")


def _static_css(css: str) -> str:
    """Mermaid默认CSS含未使用的动画规则，导出时去掉动画但保留节点样式。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    _safe_urls(css)
    if re.search(r"@import|@font-face|behavior\s*:|-moz-binding|<", css, re.I):
        raise FlowchartError("SVG样式包含不支持的活动内容。")
    while match := re.search(r"@(?:-webkit-)?keyframes\b[^{}]*\{", css, re.I):
        depth, end = 1, match.end()
        while depth and end < len(css):
            depth += (css[end] == "{") - (css[end] == "}")
            end += 1
        if depth:
            raise FlowchartError("SVG样式结构不正确。")
        css = css[: match.start()] + css[end:]
    if "@" in css:
        raise FlowchartError("SVG不支持外部或动态样式规则。")
    return re.sub(
        r"(?:^|(?<=[{;]))\s*(?:-webkit-)?(?:animation|transition)[\w-]*\s*:[^;{}]*(?:;|(?=})|$)",
        "",
        css,
        flags=re.I,
    )


def sanitize_svg(svg: str) -> str:
    """验证并序列化纯SVG；只清理默认动画CSS，其他活动内容直接拒绝。"""
    try:
        if not isinstance(svg, str) or len(svg.encode("utf-8")) > MAX_SVG_BYTES:
            raise FlowchartError("SVG为空或超过2MiB限制。")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)|<\?(?!xml\s)", svg, re.I):
            raise FlowchartError("SVG不支持DTD、实体声明或处理指令。")
        root = ET.fromstring(svg)
        if root.tag not in {"svg", f"{{{SVG_NS}}}svg"}:
            raise FlowchartError("导出内容不是SVG图示。")
        count = 0
        stack = [(root, 0)]
        while stack:
            element, depth = stack.pop()
            count += 1
            if count > 20_000 or depth > 64 or len(element.attrib) > 100:
                raise FlowchartError("SVG结构超出安全限制。")
            namespace, _, local = (
                element.tag[1:].partition("}")
                if element.tag.startswith("{")
                else ("", "", element.tag)
            )
            if namespace not in {"", SVG_NS} or local not in _SVG_ELEMENTS:
                raise FlowchartError("SVG包含脚本、动画或不支持的元素。")
            for key, value in list(element.attrib.items()):
                ns, _, attr = key[1:].partition("}") if key.startswith("{") else ("", "", key)
                if ns not in {"", SVG_NS, XLINK_NS, XML_NS}:
                    raise FlowchartError("SVG属性命名空间不受支持。")
                if attr.lower().startswith("on") or attr.lower() in {"src", "base"}:
                    raise FlowchartError("SVG不支持事件处理或外部内容。")
                if attr.lower() == "href" and not _FRAGMENT.fullmatch(value.strip()):
                    raise FlowchartError("SVG不支持外部链接。")
                _safe_urls(value)
                if attr.lower() == "style":
                    element.set(key, _static_css(value))
            if local == "style":
                if len(element):
                    raise FlowchartError("SVG样式结构不正确。")
                element.text = _static_css(element.text or "")
            stack.extend((child, depth + 1) for child in element)
        ET.register_namespace("", SVG_NS)
        ET.register_namespace("xlink", XLINK_NS)
        return ET.tostring(root, encoding="unicode")
    except FlowchartError:
        raise
    except (ET.ParseError, TypeError, UnicodeError, ValueError, RecursionError):
        raise FlowchartError("SVG结构无法解析，请重新预览后导出。") from None
