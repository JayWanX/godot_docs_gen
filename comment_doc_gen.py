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

    def parse(self, lines):
        doc = DocClass()
        pending = []
        for line in lines:
            text = self._comment_text(line)
            if text is not None:
                pending.append(text)
                continue
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
                pending = []
                continue
            # 非类/方法/成员的行：若是实际代码则丢弃注释块（文档块结束）。
            if pending and self.is_code(line):
                pending = []
        return doc

    def get_name(self):
        """从首个 class 声明解析类名；子类可覆写。"""
        raise NotImplementedError


class CppParser(ParserBase):
    """C++ 头文件解析：/// 或 ## 文档注释；类声明 / 方法 / 成员变量。"""

    COMMENT_RE = re.compile(r"^\s*(?:///+\s?(.*)|\#\#+\s?(.*))$")
    NEUTRAL_RE = re.compile(r"^\s*(?://|/\*|\*|#)")
    CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*(?::|\{)")
    GDVIRTUAL_RE = re.compile(r"^\s*GDVIRTUAL\w*\s*\(([A-Za-z_]\w*)\)")
    MEMBER_RE = re.compile(r"^\s*(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*(?:\s*<[^>]*>)?\s+([A-Za-z_]\w*)(?:\s*=\s*[^;]+?)?\s*;")

    def __init__(self, regexes=None):
        regexes = regexes or {}
        self.set("class_re", regexes.get("class_re"))
        self.set("gdvirtual_re", regexes.get("gdvirtual_re"))
        self.set("member_re", regexes.get("member_re"))

    def set(self, attr, pattern):
        if pattern:
            setattr(self, attr.upper(), re.compile(pattern))

    # C++ 方法名启发式：第一个 '(' 之前的最后一个标识符。
    @staticmethod
    def _extract_method_name(line):
        if line.strip().startswith(("/", "#", "*")):
            return None
        before_paren = line.split("(", 1)[0]
        m = re.search(r"([A-Za-z_]\w*)\s*$", before_paren)
        return m.group(1) if m else None

    def classify(self, line):
        m = self.CLASS_RE.match(line)
        if m:
            return ("class", m.group(1))
        m = self.GDVIRTUAL_RE.match(line)
        if m:
            return ("method", m.group(1))
        name = self._extract_method_name(line)
        if name:
            return ("method", name)
        m = self.MEMBER_RE.match(line)
        if m:
            return ("member", m.group(1))
        return None


class GDScriptParser(ParserBase):
    """GDScript 解析：## 文档注释；class_name / 信号 / @export var / func。"""

    COMMENT_RE = re.compile(r"^\s*#+\s?(.*)$")
    NEUTRAL_RE = re.compile(r"^\s*#")
    CLASS_NAME_RE = re.compile(r"^\s*class_name\s+([A-Za-z_]\w*)")
    FUNC_RE = re.compile(r"^\s*func\s+([A-Za-z_]\w*)\s*\(")
    MEMBER_RE = re.compile(r"^\s*(?:@export(?:\s+[A-Za-z:]+\([^)]*\))?\s+)?var\s+([A-Za-z_]\w*)")

    def classify(self, line):
        m = self.CLASS_NAME_RE.match(line)
        if m:
            return ("class", m.group(1))
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
        parser_name = "gdcscript" if lang == "gdscript" else "cpp"
        parser_cls = GDScriptParser if lang == "gdscript" else CppParser
        regexes = cfg.get("languages", {}).get(lang, {}).get("regexes")
        parser = parser_cls(regexes)
        for pattern in src["glob"].split(","):
            for path in _glob(pattern.strip()):
                results.append((path, parser))
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


def inject_doc(xml_content, doc, indent="\t"):
    """把解析出的文档数据注入 XML：类简介、方法描述、成员描述。"""
    # 类级 brief_description
    if doc.class_comment:
        xml_content = _tag_inner(
            xml_content, "<brief_description>", "</brief_description>",
            _indent(_clean(doc.class_comment), indent))

    # 方法描述：填入 <method name> 下的 <description>
    method_starts = [(m.start(), m.group(1)) for m in re.finditer(r'<method\s+name="(\w+)"', xml_content)]
    descs = [(m.start(), m.end()) for m in re.finditer(r"<description>.*?</description>", xml_content, re.DOTALL)]
    replacements = []
    for d_start, d_end in descs:
        owner = None
        for ms, mname in method_starts:
            if ms < d_start:
                owner = mname
            else:
                break
        if owner and owner in doc.method_docs:
            body = _indent(_clean(doc.method_docs[owner]), indent)
            inner_start = d_start + len("<description>")
            inner_end = d_end - len("</description>")
            replacements.append((inner_start, inner_end, body))
    for inner_start, inner_end, body in sorted(replacements, key=lambda x: x[0], reverse=True):
        xml_content = xml_content[:inner_start] + body + xml_content[inner_end:]

    # 成员描述：<member name>...</member>（含自闭合展开）
    for name, raw in doc.member_docs.items():
        body = _indent(_clean(raw), indent)
        pair = re.compile(r'(<member\s+name="%s"[^>]*>)(?P<body>.*?)(</member>)' % re.escape(name), re.DOTALL)
        m = pair.search(xml_content)
        if m:
            xml_content = xml_content[:m.start()] + m.group(1) + body + m.group(3) + xml_content[m.end():]
            continue
        selfc = re.compile(r'(<member\s+name="%s")([^>]*)/>' % re.escape(name))
        m = selfc.search(xml_content)
        if m:
            xml_content = xml_content[:m.start()] + m.group(1) + m.group(2) + ">\n" + body + "\n" + indent + "</member>" + xml_content[m.end():]
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
        if not doc.name or (not doc.class_comment and not doc.method_docs and not doc.member_docs):
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