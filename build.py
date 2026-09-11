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
    repo_root       Godot 工作区根（doctool 与相对路径的基准）
    classes_dir     模块类 XML 输出目录（相对 repo_root 或绝对路径）
    headers         注入注释的源文件 glob，多个用英文逗号分隔（相对 repo_root）
    md_dir          Markdown API 输出目录（相对 repo_root 或绝对路径）
"""

import getopt
import os
import platform
import subprocess
import sys
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


def run_doctool(cfg, godot_executable, verbose):
    repo_root = Path(cfg["repo_root"])
    if not godot_executable:
        bin_path = Path(cfg.get("engine_bin_path")) if cfg.get("engine_bin_path") else None
        if bin_path is not None and bin_path.is_file():
            # 配置直接指向可执行文件：按原样使用
            godot_executable = bin_path
        else:
            if bin_path is None or not bin_path.is_dir():
                print("Error: 未指定 Godot 路径（-g）且配置 engine_bin_path 无效（应为可执行文件或所在目录）")
                return 1
            godot_executable = find_godot(bin_path)
            if godot_executable is None:
                return 1
    if verbose:
        print("Found Godot at: %s" % godot_executable)

    # --doctool 必须作为独立参数（无前后空格），否则被当作位置参数而静默失败
    args = [str(godot_executable), "--doctool", str(repo_root)]
    if verbose:
        print("Running: ", args)
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True)
    if verbose:
        print(result.stdout)
        print("忽略与 extensions 类无关的 Godot 文件错误。")

    # doctool 只生成签名骨架，描述需从源码注释注入
    classes_dir = _resolve(repo_root, cfg["classes_dir"])
    headers = cfg.get("headers", "")
    tool = Path(__file__).resolve().parent / "comment_doc_gen.py"
    globs = [g for g in headers.split(",") if g.strip()]
    if not globs:
        print("警告: 未配置 headers，跳过注释注入")
        return 0
    cmd = [sys.executable, str(tool), "--classes-dir", str(classes_dir),
           "--headers"] + [str(_resolve(repo_root, g.strip())) for g in globs]
    if verbose:
        cmd.append("--verbose")
    subprocess.check_call(cmd)
    return 0


def run_xml_to_markdown(cfg, verbose):
    repo_root = Path(cfg["repo_root"])
    src = _resolve(repo_root, cfg["classes_dir"])
    dst = _resolve(repo_root, cfg["md_dir"])
    xml_to_markdown.process_xml_folder(src, dst, verbose)
    return 0


def print_usage():
    print("\n用法: python build.py -c 配置文件 [-d] [-a] [-h] [-v] [-g godot路径]")
    print()
    print("\t-c, --config PATH  配置文件（必填），YAML/JSON，含 engine_bin_path/repo_root/classes_dir/headers/md_dir")
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
    for key in ("repo_root", "classes_dir", "md_dir"):
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