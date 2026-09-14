#!/usr/bin/env python3
# coding: utf-8
"""comment_doc_gen — 跨项目通用的源码注释 → Godot 类文档 XML 生成器。

从 C++ / GDScript 等源码的文档注释提取类、成员、方法描述，注入到 Godot 的
doc/classes/*.xml（class.xsd 兼容）中。与具体模块解耦：通过配置文件声明
class "源文件 glob → 语言" 的映射、输出目录与各语言的声明匹配规则，可放到
任意项目直接使用。

用法：
    python comment_doc_gen.py --config config.yaml [--dry-run] [--verbose]
    python comment_doc_gen.py --headers "**/*.h" --classes-dir doc/classes

配置文件既支持 YAML（需 pyyaml）也支持 JSON；未提供 --config 时自动在当前或
脚本同级目录查找 comment_doc_gen.yaml / .yml / .json。
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

# 各语言解析器的内置默认规则，可用配置覆写（见 README/代码内注释）。


@dataclass
class DocClass:
    """从单个源文件解析出的文档数据。"""
    name: str = ""
    class_comment: list = field(default_factory=list)
    method_docs: dict = field(default_factory=dict)
    member_docs: dict = field(default_factory=dict)
    constant_docs: dict = field(default_factory=dict)
    signal_docs: dict = field(default_factory=dict)


class ParserBase:
    """行式注释解析器基类：负责注释块累积与分发，声明规则由子类实现。"""

    # 注释行匹配，必须捕获到文本（group 1）
    COMMENT_RE = re.compile(r"")
    # 非文档注释（不应中断当前注释块，也不触发生成）
    NEUTRAL_RE = re.compile(r"")

    def classify(self, line):
        """返回 (kind, name) 或 None。kind ∈ {class, method, member}。"""
        raise NotImplementedError

    def is_code(self, line):
        """判断一行是否为实际代码（相对空行/注释）。"""
        s = line.strip()
        return bool(s) and not self.NEUTRAL_RE.match(line)

    def _comment_text(self, line):
        m = self.COMMENT_RE.match(line)
        return m.group(1).strip() if m else None

    def _signal_marker(self, line):
        """从注释行提取信号名；非信号声明返回 None。子类按需覆写。"""
        return None

    def parse(self, lines):
        doc = DocClass()
        pending = []
        cur_signal = None
        for line in lines:
            sig = self._signal_marker(line)
            if sig is not None:
                cur_signal = sig
                doc.signal_docs[sig] = []
                pending = []
                continue
            text = self._comment_text(line)
            if text is not None:
                if cur_signal is not None:
                    doc.signal_docs[cur_signal].append(text)
                else:
                    pending.append(text)
                continue
            if cur_signal is not None:
                # 信号注释块遇到代码行即结束。
                if self.is_code(line):
                    cur_signal = None
            kind_name = self.classify(line)
            if kind_name:
                kind, name = kind_name
                if kind == "class":
                    doc.name = name
                    if pending:
                        doc.class_comment = pending
                elif kind == "method" and pending:
                    doc.method_docs[name] = list(pending)
                elif kind == "member" and pending:
                    doc.member_docs[name] = list(pending)
                elif kind == "signal" and pending:
                    doc.signal_docs[name] = list(pending)
                elif kind == "constant":
                    if pending:
                        doc.constant_docs[name] = list(pending)
                    else:
                        trailing = self._trailing_doc(line)
                        if trailing:
                            doc.constant_docs[name] = trailing
                pending = []
                continue
            # 非类/方法/成员的行：若是实际代码则丢弃注释块（文档块结束）。
            if pending and self.is_code(line):
                pending = []
        return doc

    def get_name(self):
        """从首个 class 声明解析类名；子类可覆写。"""
        raise NotImplementedError

    def _trailing_doc(self, line):
        """提取行尾文档注释。默认不支持，子类按各自注释符号覆写。"""
        return None


class CppParser(ParserBase):
    """C++ 头文件解析：/// 或 ## 文档注释；类/方法/成员变量/枚举常量。"""

    COMMENT_RE = re.compile(r"^\s*(?:///+\s?(.*)|\#\#+\s?(.*))$")
    NEUTRAL_RE = re.compile(r"^\s*(?://|/\*|\*|#)")
    CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*(?::|\{)")
    GDVIRTUAL_RE = re.compile(r"^\s*GDVIRTUAL\w*\s*\(([A-Za-z_]\w*)\)")
    MEMBER_RE = re.compile(r"^\s*(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*(?:\s*<[^>]*>)?\s+([A-Za-z_]\w*)(?:\s*=\s*[^;]+?)?\s*;")
    ENUM_OPEN_RE = re.compile(r"^\s*enum(?:\s+class)?(?:\s+\w*)?\s*\{")
    ENUM_END_RE = re.compile(r"^\s*\}")
    CONSTANT_RE = re.compile(r"^\s*([A-Za-z_]\w*)")

    def __init__(self, regexes=None):
        regexes = regexes or {}
        self._in_enum = False
        self.set("class_re", regexes.get("class_re"))
        self.set("gdvirtual_re", regexes.get("gdvirtual_re"))
        self.set("member_re", regexes.get("member_re"))
        self.set("enum_open_re", regexes.get("enum_open_re"))
        self.set("constant_re", regexes.get("constant_re"))

    def set(self, attr, pattern):
        if pattern:
            setattr(self, attr.upper(), re.compile(pattern))

    def _trailing_doc(self, line):
        """提取 C++ 行尾 `///` 文档注释。"""
        m = re.search(r"//+\s+(.+?)\s*$", line)
        return [m.group(1).strip()] if m else None

    def _signal_marker(self, line):
        """提取 `/// @signal <name>` 形式的信号声明。"""
        m = re.match(r"^\s*//+\s*@signal\s+([A-Za-z_]\w*)\s*$", line)
        return m.group(1) if m else None

    # C++ 方法名启发式：第一个 '(' 之前的最后一个标识符。
    @staticmethod
    def _extract_method_name(line):
        if line.strip().startswith(("/", "#", "*")):
            return None
        eq = line.find("=")
        op = line.find("(")
        # `=` 出现在 `(` 之前：是成员初始化 `name = Type(...)`，而非方法声明。
        if eq != -1 and (op == -1 or eq < op):
            return None
        before_paren = line.split("(", 1)[0]
        m = re.search(r"([A-Za-z_]\w*)\s*$", before_paren)
        return m.group(1) if m else None

    def classify(self, line):
        m = self.CLASS_RE.match(line)
        if m:
            return ("class", m.group(1))
        if self._in_enum:
            # 枚举体内：收尾行退出状态，标识符行归为枚举常量
            if self.ENUM_END_RE.match(line):
                self._in_enum = False
                return None
            m = self.CONSTANT_RE.match(line)
            if m:
                return ("constant", m.group(1))
            return None
        if self.ENUM_OPEN_RE.match(line):
            self._in_enum = True
            return None
        m = self.GDVIRTUAL_RE.match(line)
        if m:
            return ("method", m.group(1))
        name = self._extract_method_name(line)
        if name:
            return ("method", name)
        m = self.MEMBER_RE.match(line)
        if m:
            name = m.group(1)
            # Godot 模块惯例：私有成员 `_field` 经 getter/setter 暴露为 `field`，
            # XML 中属性名不带前导下划线，这里去掉以匹配。
            if name.startswith("_"):
                name = name[1:]
            return ("member", name)
        return None


class GDScriptParser(ParserBase):
    """GDScript 解析：## 文档注释；class_name / 信号 / @export var / func。"""

    COMMENT_RE = re.compile(r"^\s*#+\s?(.*)$")
    NEUTRAL_RE = re.compile(r"^\s*#")
    CLASS_NAME_RE = re.compile(r"^\s*class_name\s+([A-Za-z_]\w*)")
    SIGNAL_RE = re.compile(r"^\s*signal\s+([A-Za-z_]\w*)\b")
    FUNC_RE = re.compile(r"^\s*func\s+([A-Za-z_]\w*)\s*\(")
    MEMBER_RE = re.compile(r"^\s*(?:@export(?:\s+[A-Za-z:]+\([^)]*\))?\s+)?var\s+([A-Za-z_]\w*)")

    def _trailing_doc(self, line):
        """提取 GDScript 行尾 `#` 文档注释。"""
        m = re.search(r"#+\s+(.+?)\s*$", line)
        return [m.group(1).strip()] if m else None

    def classify(self, line):
        m = self.CLASS_NAME_RE.match(line)
        if m:
            return ("class", m.group(1))
        m = self.SIGNAL_RE.match(line)
        if m:
            return ("signal", m.group(1))
        m = self.FUNC_RE.match(line)
        if m:
            return ("method", m.group(1))
        m = self.MEMBER_RE.match(line)
        if m:
            return ("member", m.group(1))
        return None


# 可扩展的语言注册表：extension marker → 解析器
PARSERS = {
    ".h": CppParser,
    ".hpp": CppParser,
    ".gd": GDScriptParser,
}


# ---------- 配置 ----------

BUILTIN_CONFIG = {
    "classes_dir": "doc/classes",
    "schema": None,
    "sources": [
        {"glob": "**/*.h", "language": "cpp"},
        {"glob": "**/*.hpp", "language": "cpp"},
        {"glob": "**/*.gd", "language": "gdscript"},
    ],
}


def _load_yaml_text(text):
    # pyyaml 可用则走 YAML；否则退化为按行解析简单的 key: value 两层结构。
    try:
        import yaml  # noqa: PLC0415
        return yaml.safe_load(text)
    except ImportError:
        pass
    return _minimal_yaml(text)


def _minimal_yaml(text):
    """极简两层解析：冒号分隔键值 + 顶层列表项（- glob/language）。"""
    data = {"sources": []}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            entry = {}
            for part in raw.split(" - ")[1:]:
                if ":" in part:
                    k, v = part.split(":", 1)
                    entry[k.strip()] = v.strip().strip("\"'")
            if entry:
                data["sources"].append(entry)
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip().strip("\"'")
    return data


def load_config(path):
    if path is None:
        for candidate in ("comment_doc_gen.yaml", "comment_doc_gen.yml", "comment_doc_gen.json"):
            if os.path.exists(candidate):
                path = candidate
                break
    if path is None:
        return BUILTIN_CONFIG

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        data = json.loads(text)
    else:
        data = _load_yaml_text(text)

    cfg = dict(BUILTIN_CONFIG)
    cfg.update(data or {})
    return cfg


# ---------- 遍历源文件 ----------

def discover_sources(cfg, headers_override=None):
    """按配置的 sources 匹配文件，返回 [(path, ParserClass)]。"""
    if headers_override:
        results = []
        for pattern in headers_override:
            for path in _glob(pattern):
                parser_cls = PARSERS.get(os.path.splitext(path)[1])
                if parser_cls:
                    results.append((path, parser_cls))
        return results

    results = []
    for src in cfg.get("sources", []):
        lang = src.get("language", "cpp")
        parser_cls = GDScriptParser if lang == "gdscript" else CppParser
        for pattern in src["glob"].split(","):
            for path in _glob(pattern.strip()):
                results.append((path, parser_cls))
    return results


def _glob(pattern):
    """基于 fnmatch 的简单 glob：支持绝对/相对文件路径与 ** 递归匹配。"""
    import fnmatch as _fn
    if os.path.isfile(pattern):
        return [pattern]
    base = None
    clean = pattern
    if "**" in pattern:
        root, _, tail = pattern.partition("**")
        base = root if root else "."
        clean = tail.lstrip("/\\")
    else:
        base = "."
        clean = pattern.lstrip("/\\")
    matches = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".venv", "node_modules")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), base)
            if _fn.fnmatch(rel, clean) or _fn.fnmatch(rel, os.path.basename(pattern)):
                matches.append(os.path.normpath(os.path.join(dirpath, name)))
    return matches


# ---------- 注入 ----------

def _clean(raw_lines):
    return "\n".join(l.strip() for l in raw_lines if l.strip()).strip()


def _indent(text, indent):
    return "\n".join(indent + t if t.strip() else "" for t in text.split("\n"))


def _tag_inner(xml_content, open_tag, close_tag, body):
    """替换 <open>...</close> 的 inner。返回替换后内容；找不到原样返回。"""
    pattern = re.compile(
        r"(%s)(?P<body>.*?)(%s)" % (re.escape(open_tag), re.escape(close_tag)), re.DOTALL)
    m = pattern.search(xml_content)
    if not m:
        return xml_content
    return xml_content[:m.start()] + m.group(1) + body + m.group(3) + xml_content[m.end():]


def _line_indent(xml_content, pos):
    """pos 所在行的行首空白缩进。"""
    line_start = xml_content.rfind("\n", 0, pos) + 1
    return xml_content[line_start:pos]


def _inner_lines(raw_lines):
    """整理注释行：去空行、去行首尾空白。"""
    return [l.strip() for l in raw_lines if l.strip()]


def _element_inner(xml_content, open_pos, raw_lines, fallback):
    """构造元素内嵌内容：每注释行占一行，缩进比 open 标签行深一级。
    [return] 含首尾换行与对齐的 inner；配合元素结束标签置于 open 标签行缩进处。"""
    lines = _inner_lines(raw_lines)
    tag_indent = _line_indent(xml_content, open_pos)
    content_indent = (tag_indent + "\t") if tag_indent else fallback
    return "\n" + content_indent + ("\n" + content_indent).join(lines) + "\n" + tag_indent


def inject_doc(xml_content, doc, indent="\t"):
    """把解析出的文档数据注入 XML：类简介、方法描述、成员描述。"""
    # 类级 brief_description
    if doc.class_comment:
        xml_content = _tag_inner(
            xml_content, "<brief_description>", "</brief_description>",
            _indent(_clean(doc.class_comment), indent))

    # 方法描述：只填 <method name>...</method> 块内部的 <description>。
    # 信号描述没有独立注释来源，统一清空，杜绝方法块注释误串入 <signal>。
    replacements = []
    for method_body in re.finditer(r'<method\s+name="(\w+)"[^>]*>.*?</method>', xml_content, re.DOTALL):
        mname = method_body.group(1)
        if mname not in doc.method_docs:
            continue
        dm = re.search(r"(<description>)(.*?)(</description>)", method_body.group(0), re.DOTALL)
        if not dm:
            continue
        offset = method_body.start()
        inner = _element_inner(xml_content, offset + dm.start(1), doc.method_docs[mname], indent + "\t")
        replacements.append((offset + dm.start(2), offset + dm.end(3) - len("</description>"), inner))
    for sig in re.finditer(r"<signal\s+name=\"(\w+)\"[^>]*>.*?</signal>", xml_content, re.DOTALL):
        sname = sig.group(1)
        dm = re.search(r"(<description>)(.*?)(</description>)", sig.group(0), re.DOTALL)
        if not dm:
            continue
        open_pos = sig.start() + dm.start(1)
        tag_indent = _line_indent(xml_content, open_pos)
        if sname in doc.signal_docs and doc.signal_docs[sname]:
            inner = _element_inner(xml_content, open_pos, doc.signal_docs[sname], indent + "\t")
        else:
            inner = "\n" + tag_indent
        replacements.append((sig.start() + dm.start(2),
                             sig.start() + dm.end(3) - len("</description>"), inner))
    for inner_start, inner_end, inner in sorted(replacements, key=lambda x: x[0], reverse=True):
        xml_content = xml_content[:inner_start] + inner + xml_content[inner_end:]

    # 成员描述：<member name>...</member>（含自闭合展开）
    xml_content = _inject_elements(xml_content, doc.member_docs, "member", indent)
    # 枚举/类常量描述：<constant name>...</constant>
    xml_content = _inject_elements(xml_content, doc.constant_docs, "constant", indent)
    return xml_content


def _inject_elements(xml_content, docs, tag, indent):
    """把 {name: 描述} 注入 <tag name>...</tag>，自闭合则展开。内容行独立缩进。"""
    for name, raw in docs.items():
        pair = re.compile(r'(<%s\s+name="%s"[^>]*>)(?P<body>.*?)(</%s>)'
                          % (re.escape(tag), re.escape(name), re.escape(tag)), re.DOTALL)
        m = pair.search(xml_content)
        if m:
            inner = _element_inner(xml_content, m.start(), raw, indent + "\t")
            xml_content = xml_content[:m.start()] + m.group(1) + inner + m.group(3) + xml_content[m.end():]
            continue
        selfc = re.compile(r'(<%s\s+name="%s")([^>]*)/>' % (re.escape(tag), re.escape(name)))
        m = selfc.search(xml_content)
        if m:
            tag_indent = _line_indent(xml_content, m.start())
            content_indent = (tag_indent + "\t") if tag_indent else (indent + "\t")
            block = ("\n" + content_indent).join(_inner_lines(raw))
            xml_content = (xml_content[:m.start()] + m.group(1) + m.group(2).rstrip()
                           + ">\n" + content_indent + block + "\n" + tag_indent
                           + "</%s>" % tag + xml_content[m.end():])
    return xml_content


def _rewrite_schema_attribute(xml_content, schema, classes_dir):
    """将 XML 根元素的 noNamespaceSchemaLocation 重写为指向 schema 的相对路径。"""
    if not schema or not classes_dir:
        return xml_content
    href = os.path.relpath(os.path.abspath(schema), start=os.path.abspath(classes_dir)).replace("\\", "/")
    return re.sub(
        r'(<class\s+[^>]*?xsi:noNamespaceSchemaLocation=")[^"]*(")',
        lambda m: m.group(1) + href + m.group(2),
        xml_content, count=1)


def match_class_xml(xml_path, doc, verbose=False, schema=None, classes_dir=None):
    """对单个 XML 文件注入单个源文件的文档。返回是否写入。"""
    if not os.path.exists(xml_path):
        if verbose:
            print("  (skip, missing) %s" % xml_path)
        return False
    with open(xml_path, "r", encoding="utf-8") as f:
        content = f.read()
    cm = re.search(r'<class\s+name="([A-Za-z_]\w*)"', content)
    if not cm:
        return False
    new_content = inject_doc(content, doc)
    new_content = _rewrite_schema_attribute(new_content, schema, classes_dir)
    if new_content == content:
        return False
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


# ---------- CLI ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description="源码注释 → Godot 类文档 XML 生成器")
    ap.add_argument("--config", help="YAML/JSON 配置文件路径")
    ap.add_argument("--headers", nargs="*", help="覆盖源文件 glob（按扩展名自动选语言）")
    ap.add_argument("--classes-dir", help="覆盖类 XML 输出目录")
    ap.add_argument("--schema", help="class.xsd 路径（相对或绝对），重写 XML 的 noNamespaceSchemaLocation")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要处理的项目，不写文件")
    ap.add_argument("--verbose", action="store_true", help="打印详细过程")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    classes_dir = args.classes_dir or cfg.get("classes_dir") or "doc/classes"
    schema = args.schema if args.schema is not None else cfg.get("schema")

    sources = discover_sources(cfg, args.headers)
    if not sources:
        print("未匹配到任何源文件")
        return 1

    patched = 0
    for path, parser in sources:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            print("读取失败 %s: %s" % (path, e))
            continue
        doc = parser().parse(lines)
        if not doc.name or (not doc.class_comment and not doc.method_docs and not doc.member_docs and not doc.constant_docs):
            continue
        xml_path = os.path.join(classes_dir, doc.name + ".xml")
        if args.verbose:
            print("source: %s -> %s (members=%d methods=%d)" % (
                path, xml_path, len(doc.member_docs), len(doc.method_docs)))
        if args.dry_run:
            continue
        if match_class_xml(xml_path, doc, args.verbose, schema=schema, classes_dir=classes_dir):
            patched += 1
    print("Patched %d XML files" % patched)
    return 0


if __name__ == "__main__":
    sys.exit(main())