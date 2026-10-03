#!/usr/bin/env python3
# coding: utf-8
"""一键构建 Godot 模块的 API 文档（跨项目通用）。

流程：
    1. [-d] 运行 Godot doctool 生成类 XML 骨架
    2. [-d] 用 comment_doc_gen 从源码注释注入描述
    3. [-a] 用 xml_to_markdown 把 XML 转换为 Markdown API 文档

所有路径通过配置文件（YAML/JSON）提供，脚本不依赖自身位置，可在任意项目复用。

用法：
    python build.py -c docs.yaml [-d] [-a] [-v] [-g godot_binary]

配置字段：
    engine_bin_path  已编译含本模块的 Godot 编辑器路径（可执行文件或所在目录，可被 -g 覆盖）
    project_root    模块项目根（doctool 与相对路径的基准）；缺省取当前工作目录
    classes_dir     模块类 XML 输出目录（相对 project_root 或绝对路径）
    headers         注入注释的源文件 glob，多个用英文逗号分隔（相对 project_root）
    md_dir          Markdown API 输出目录（相对 project_root 或绝对路径）
"""

import getopt
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 使同目录兄弟模块可作为顶层模块导入（脚本直跑或 -m 均可用）
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import xml_to_markdown  # noqa: E402


def _load_config(path):
    """读取 YAML/JSON 配置，返回简单 dict；未提供时返回空字典。"""
    if not path:
        return {}
    if not os.path.exists(path):
        sys.exit("配置文件不存在: %s" % path)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        import json
        data = json.loads(text)
    else:
        try:
            import yaml
            data = yaml.safe_load(text)
        except ImportError:
            data = _minimal_yaml(text)
    return data or {}


def _minimal_yaml(text):
    """pyyaml 缺失时的极简两层 YAML 解析（键: 值为主，忽略列表项）。"""
    data = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line and not line.startswith("-"):
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip().strip("\"'")
    return data


def find_godot(bin_dir):
    """在 bin_dir 中寻找可用的 Godot 编辑器二进制（优先 console 构建，其次最新）。"""
    prefix = "godot"
    os_prefix = ""
    suffix = ""
    if sys.platform in ("win32", "cygwin"):
        os_prefix = ".windows"
        suffix = ".exe"
    elif sys.platform == "darwin":
        os_prefix = ".macos"
    else:
        os_prefix = ".linuxbsd"

    arch = ".x86_64"
    machine = platform.machine().lower()
    if machine == "arm64":
        arch = ".arm64"
    elif machine == "riscv64":
        arch = ".rv64"

    # 本项目编译为 editor + double 精度，需同时匹配纯 editor 与 editor.double 变体
    names = [
        prefix + os_prefix + ".editor" + arch + suffix,
        prefix + os_prefix + ".editor.dev" + arch + suffix,
        prefix + os_prefix + ".editor.double" + arch + suffix,
        prefix + os_prefix + ".editor.dev.double" + arch + suffix,
        prefix + os_prefix + ".editor" + arch + ".console" + suffix,
        prefix + os_prefix + ".editor.dev" + arch + ".console" + suffix,
        prefix + os_prefix + ".editor.double" + arch + ".console" + suffix,
        prefix + os_prefix + ".editor.dev.double" + arch + ".console" + suffix,
    ]

    binaries = []
    for name in names:
        path = bin_dir / name
        if path.is_file():
            binaries.append((path, os.path.getmtime(path)))
    if not binaries:
        print("Error: 在 %s 中未找到合适的 Godot 编辑器二进制" % bin_dir)
        return None

    # Windows 下 doctool 需控制台输出，有 console 构建则优先
    console = [b for b in binaries if ".console" in b[0].name]
    chosen = sorted(console, key=lambda t: t[1]) if console else sorted(binaries, key=lambda t: t[1])
    return chosen[-1][0]


def _resolve(base, value):
    """若 value 非绝对路径，则相对 base 解析。"""
    p = Path(value)
    return p if p.is_absolute() else base / p


def _project_root(cfg):
    """项目根：配置缺省时取当前工作目录。"""
    root = cfg.get("project_root")
    return Path(root) if root else Path.cwd()


def _sync_schema(project_root, verbose):
    """把工具集自带 class.xsd 复制到 <project_root>/doc/class.xsd，返回项目内副本路径。

    使生成的 XML 引用项目内的 schema，避免引用项目外文件。
    """
    src = Path(__file__).resolve().parent / "schema" / "class.xsd"
    if not src.is_file():
        print("警告: 未找到工具集自带 schema: %s" % src)
        return None
    dst = project_root / "doc" / "class.xsd"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if verbose:
            print("schema -> %s" % dst)
    except OSError as e:
        print("警告: 复制 schema 失败: %s" % e)
    return dst


def _prune_classes(classes_dir, doc_classes):
    """仅保留本模块的类 XML，删除 doctool 倾泻到 classes_dir 的其它引擎类。

    [param classes_dir] 类 XML 目录[br]
    [param doc_classes] 本模块公开类名列表。
    """
    if not doc_classes:
        return
    keep_set = set(doc_classes)
    for path in classes_dir.rglob("*.xml"):
        if path.stem not in keep_set:
            path.unlink()


def _fix_class_schema(classes_dir):
    """把 doctool 写的错误 schema 相对路径改写为项目内副本路径。

    doctool 恒把 schemaLocation 写成面向模块位于引擎源码 modules/ 布局的
    ../../../doc/class.xsd；custom_modules/* 上溯三级越界无法解析，统一改写为
    ../class.xsd（即 <project_root>/doc/class.xsd）。与注释注入解耦，无条件执行，
    使无源码注释的模块（如 voxel 的上游手写描述）也能得到正确 schema。
    [param classes_dir] 类 XML 输出目录。
    """
    for xml_path in Path(classes_dir).glob("*.xml"):
        text = xml_path.read_text(encoding="utf-8")
        fixed = text.replace(
            'xsi:noNamespaceSchemaLocation="../../../doc/class.xsd"',
            'xsi:noNamespaceSchemaLocation="../class.xsd"')
        if fixed != text:
            # 显式 newline="\n"：依赖 Python 默认换行翻译会在 Windows 上写成 CRLF
            xml_path.write_text(fixed, encoding="utf-8", newline="\n")


def _discover_doc_classes(project_root):
    """从模块根 config.py 的 get_doc_classes() 读取本模块公开类清单。

    采用 AST 静态解析而非 import，避免执行模块配置的任意代码。
    [param project_root] 模块项目根目录[br]
    [return] 类名列表，未解析到返回空列表。
    """
    doc_classes: list[str] = []
    cfg_path = project_root / "config.py"
    if not cfg_path.is_file():
        return doc_classes
    import ast
    tree = ast.parse(cfg_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_doc_classes":
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, (ast.List, ast.Tuple)):
                    doc_classes = [e.value for e in stmt.value.elts
                                   if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    if doc_classes:
                        return doc_classes
    return doc_classes


def _iter_sibling_doc_dirs(project_root):
    """枚举与目标模块同目录的兄弟模块（含 config.py 与 doc/classes 的独立模块仓库）。

    Godot doctool 会把目标模块跑 -d 时**原地重写**所有已注册模块的 doc/classes
    （doc_data_class_path.gen.h 存绝对路径，main.cpp 只给相对路径补 doc_tool_path），
    因此同一 <父目录> 下其它模块的类 XML 会被连带改写。这里收集这些兄弟模块，
    供 doctool 之后逐个修回被写坏的 schemaLocation，保证一次 -d 名义上只影响目标模块。
    [param project_root] 目标模块项目根[br]
    [return] 兄弟模块 (module_root, doc_classes_dir) 生成器。
    """
    parent = project_root.parent
    if not parent.is_dir():
        return
    for child in sorted(parent.iterdir()):
        if child == project_root or not child.is_dir():
            continue
        if (child / "config.py").is_file() and (child / "doc" / "classes").is_dir():
            yield child, child / "doc" / "classes"


def _acquire_lock(project_root):
    """取 doctool 互斥锁，返回 (锁文件路径, 是否成功)。

    doctool 会原地重写**所有**注册模块的 doc/classes，而 --doctool 目标目录只承接
    其中本模块那份。两个进程并发跑时后跑者会把先跑者的还原结果重新覆盖，事后无法
    区分是谁污染的。故用 O_EXCL 串行化；锁残留说明上次运行被异常终止。
    锁放系统临时目录而非模块仓库内，避免污染各模块的 git status。
    [param project_root] 模块项目根[br]
    [return] (锁文件路径, 是否取得锁)。
    """
    key = hashlib.md5(str(project_root).lower().encode("utf-8")).hexdigest()[:12]
    lock = Path(tempfile.gettempdir()) / ("godot_docs_lock_%s_%s" % (project_root.name, key))
    if lock.exists():
        return lock, False
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, OSError):
        return lock, False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return lock, True


def _release_lock(lock):
    """释放 doctool 互斥锁，失败只告警不抛（清理阶段不应掩盖主流程错误）。"""
    try:
        lock.unlink()
    except OSError as e:
        print("警告: 未能删除 doctool 锁 %s（%s），请手动清理" % (lock, e))


def run_doctool(cfg, godot_executable, verbose):
    project_root = _project_root(cfg)

    # doctool 原地重写所有注册模块的 doc/classes，并发会互相污染，先串行化。
    lock, acquired = _acquire_lock(project_root)
    if not acquired:
        print("Error: 存在未清理的 doctool 锁 %s" % lock)
        print("       另一个 %s 的 doctool 可能正在运行；确认无运行中的进程后删除该文件再重试。"
              % project_root.name)
        return 1

    if not godot_executable:
        bin_path = Path(cfg.get("engine_bin_path")) if cfg.get("engine_bin_path") else None
        if bin_path is not None and bin_path.is_file():
            # 配置直接指向可执行文件：按原样使用
            godot_executable = bin_path
        else:
            if bin_path is None or not bin_path.is_dir():
                print("Error: 未指定 Godot 路径（-g）且配置 engine_bin_path 无效（应为可执行文件或所在目录）")
                _release_lock(lock)
                return 1
            godot_executable = find_godot(bin_path)
            if godot_executable is None:
                _release_lock(lock)
                return 1
    if verbose:
        print("Found Godot at: %s" % godot_executable)

    # doctool 会把引擎全部内置模块（csg/gdscript/gltf 等）的类文档按相对路径
    # modules/<m>/doc_classes 写到 --doctool 目标目录下；若直接在模块根跑会虚构出 modules/。
    # 因此用临时目录承接输出，跑完仅把本模块的类 XML 合并回 classes_dir，其余全部丢弃。
    # 该目录一次产出两千余个文件，放系统临时目录且**不回收**：本机沙箱会拦下这种规模的
    # 删除（SAFE_DELETE_BULK_CONFIRM_REQUIRED，命中即终止进程），跑完再删等于白跑一次。
    scratch = Path(tempfile.mkdtemp(prefix="godot_docs_scratch_"))
    if verbose:
        print("Scratch: %s（跑完不自动清理，可手动删除）" % scratch)

    # doctool 会原地重写所有注册模块的 doc/classes（含兄弟模块），故跑完要逐个修回。
    siblings = list(_iter_sibling_doc_dirs(project_root))

    try:
        # --doctool 必须作为独立参数（无前后空格），否则被当作位置参数而静默失败
        args = [str(godot_executable), "--doctool", str(scratch)]
        if verbose:
            print("Running: ", args)
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True)
        if verbose:
            print(result.stdout)
            print("忽略与模块类无关的 Godot 文件错误。")

        # doctool 只生成签名骨架，描述需从源码注释注入
        classes_dir = _resolve(project_root, cfg["classes_dir"])

        # 从临时索引中仅保留本模块类 XML 并合并进 classes_dir
        index_dir = scratch / "doc" / "classes"
        if index_dir.is_dir():
            _prune_classes(index_dir, _discover_doc_classes(project_root))
            classes_dir.mkdir(parents=True, exist_ok=True)
            for xml_path in index_dir.glob("*.xml"):
                shutil.copy2(xml_path, classes_dir / xml_path.name)
    finally:
        # doctool 会原地重写所有注册模块的 doc/classes：doc_data_class_path.gen.h 存的是
        # 绝对路径，而 main.cpp 只给相对路径补 doc_tool_path，故绝对路径原样落地。
        # 实测它对兄弟模块只改坏 schemaLocation 这一行（描述与成员都被 merge_from 保留），
        # 因此跑完逐个幂等修回即等价于「未污染」，无需整目录快照回拷——后者需要 rmtree
        # 整个目录，会被本机批量删除保护拦下并静默失败（曾致 178 个文件被污染）。
        # 收尾必须包在嵌套 finally 里：清理本身失败不能打断主流程，也不能漏掉释放锁。
        try:
            for _, cls_dir in siblings:
                if verbose:
                    print("修回兄弟模块 schemaLocation: %s" % cls_dir)
                _fix_class_schema(cls_dir)
        finally:
            _release_lock(lock)

    # doctool 恒写坏 schema 相对路径，无论是否注入注释都需修正
    _fix_class_schema(classes_dir)

    headers = cfg.get("headers", "")
    tool = Path(__file__).resolve().parent / "comment_doc_gen.py"
    globs = [g for g in headers.split(",") if g.strip()]
    if not globs:
        print("警告: 未配置 headers，跳过注释注入")
        return 0

    # 把工具集自带 schema 复制进项目内（<project_root>/doc/class.xsd），
    # 让 XML 引用本项目内的副本，避免引用项目外文件。
    local_schema = _sync_schema(project_root, verbose)
    cmd = [sys.executable, str(tool), "--classes-dir", str(classes_dir),
           "--headers"] + [str(_resolve(project_root, g.strip())) for g in globs]
    if local_schema:
        cmd += ["--schema", str(local_schema)]
    if verbose:
        cmd.append("--verbose")
    subprocess.check_call(cmd)
    return 0


def run_xml_to_markdown(cfg, verbose):
    project_root = _project_root(cfg)
    src = _resolve(project_root, cfg["classes_dir"])
    dst = _resolve(project_root, cfg["md_dir"])
    xml_to_markdown.process_xml_folder(src, dst, verbose)
    return 0


def print_usage():
    print("\n用法: python build.py -c 配置文件 [-d] [-a] [-h] [-v] [-g godot路径]")
    print()
    print("\t-c, --config PATH  配置文件（必填），YAML/JSON，含 engine_bin_path/project_root/classes_dir/headers/md_dir")
    print("\t-d                 运行 Godot doctool 更新 XML 类数据，并注入源码注释")
    print("\t-a                 从 XML 类数据生成 Markdown API 文档")
    print("\t-h, --help         打印帮助")
    print("\t-v                 Verbose，打印更多过程信息")
    print("\t-g                 指定 Godot 可执行文件路径，覆盖配置 engine_bin_path")
    print()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    config_path = ""
    verbose = False
    must_doctool = False
    must_md = False
    godot_executable = ""

    try:
        opts, _ = getopt.getopt(argv, "dahvg:c:", ["help", "config="])
    except getopt.error as msg:
        print("Error: ", msg)
        print_usage()
        return 1

    for opt, arg in opts:
        if opt == '-d':
            must_doctool = True
        elif opt == '-a':
            must_md = True
        elif opt in ('-h', '--help'):
            print_usage()
            return 0
        elif opt == '-v':
            verbose = True
        elif opt == '-g':
            godot_executable = arg
        elif opt in ('-c', '--config'):
            config_path = arg

    if not config_path:
        print("缺少必填参数: 请用 -c 指定配置文件")
        print_usage()
        return 1

    cfg = _load_config(config_path)
    for key in ("classes_dir", "md_dir"):
        if key not in cfg:
            print("错误: 配置缺少必填项 %s" % key)
            return 1

    rc = 0
    if must_doctool:
        rc = run_doctool(cfg, godot_executable, verbose) or rc
    if must_md:
        rc = run_xml_to_markdown(cfg, verbose) or rc
    if not must_doctool and not must_md:
        print("未指定任何操作。")
        print_usage()
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())