# comment_doc_gen — 跨项目通用源码注释 → Godot 类文档生成器

从 C++ / GDScript 源码的文档注释中提取类、成员、方法描述，注入到 Godot 的
`doc/classes/*.xml`（`class.xsd` 兼容），用于补充官方 doctool 只生成方法签名
骨架、不提取模块自定义注释的缺口。与具体模块解耦，可通过配置文件在任意项目
直接使用。

本工具是跨项目工具集 `godot_docs_gen` 的一部分（[本仓库](.) ），该仓库同时提供
XML→Markdown 转换与一键构建（见 [集成到构建流程](#集成到构建流程)）。

## 目录

- [功能特性](#功能特性)
- [安装要求](#安装要求)
- [快速开始](#快速开始)
- [命令行参数](#命令行参数)
- [配置文件](#配置文件)
- [注释书写约定](#注释书写约定)
- [工作原理](#工作原理)
- [集成到构建流程](#集成到构建流程)

## 功能特性

- **多语言解析**：内置 `CppParser`（`///` 或 `##`）与 `GDScriptParser`（`##`），
  通过 `PARSERS` 注册表按扩展名自动路由。
- **配置驱动**：YAML/JSON 配置声明 `sources`（glob → 语言）、`classes_dir` 及
  各语言自定义正则，不修改任何代码即可适配新项目。
- **类名定位**：按解析出的类名匹配 `{类名}.xml`，与 doctool 的命名规则一致
  （如 `dict_utils.h` → 注入 `DictUtils.xml`）。
- **幂等**：已填充的描述不会被重复覆盖；缺失对应 XML/标签的元素自动跳过。
- **空运行**：`--dry-run` 只打印将要处理的项目，不写入文件。

## 安装要求

- Python 3.8+，仅标准库（`argparse`、`re`、`dataclasses`、可选 `pyyaml`）。
- `pyyaml` 非必需：未安装时自动退化为内置的极简两层 YAML 解析器。

## 快速开始

```bash
# 在仓库根目录执行

# 使用内置默认（扫描当前目录 **/*.{h,hpp,gd}，输出到 doc/classes）
python comment_doc_gen.py

# 显式指定源文件 glob 与输出目录（在接入本工具的模块项目根目录执行）
python /path/to/godot_docs_gen/comment_doc_gen.py \
    --headers "custom_modules/extensions/**/*.h" \
    --classes-dir "custom_modules/extensions/doc/classes"

# 使用配置文件
python comment_doc_gen.py --config docgen.yaml

# 预览将处理的项目，不写入
python comment_doc_gen.py --config docgen.yaml --dry-run --verbose
```

> 说明：命令行示例中带 `../` 或 `<` `>` 及说明段落的路径均为相对路径或占位符，
> 不含开发者机器的绝对路径。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | YAML/JSON 配置文件路径；省略时自动查找 `comment_doc_gen.{yaml,yml,json}`，仍无则用内置默认 |
| `--headers GLOB` | 覆盖源文件 glob（可多个），按扩展名自动选语言 |
| `--classes-dir DIR` | 覆盖类 XML 输出目录 |
| `--schema PATH` | `class.xsd` 路径（相对或绝对）；注入后把 XML 的 `noNamespaceSchemaLocation` 重写指向它 |
| `--dry-run` | 只打印将处理的项目，不写文件 |
| `--verbose` | 打印每个源文件 → XML 的映射与统计 |

## 配置文件

配置同时支持 YAML 与 JSON。示例 `docgen.yaml`：

```yaml
# 类 XML 输出目录
classes_dir: doc/classes
# 可选：class.xsd 路径；注入后重写每个 XML 的 noNamespaceSchemaLocation 指向它
schema: schema/class.xsd
# 源文件 → 语言映射；glob 可含 ** 递归匹配，逗号分隔多个模式
sources:
  - glob: "**/*.h, **/*.hpp"
    language: cpp
  - glob: "**/*.gd"
    language: gdscript
# 各语言可覆写解析正则（键名对应解析器使用的规则名）
languages:
  cpp:
    regexes:
      class_re: '^\s*class\s+([A-Za-z_]\w*)\s*(?::|\{)'
      member_re: '^\s*(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*(?:\s*<[^>]*>)?\s+([A-Za-z_]\w*)(?:\s*=\s*[^;]+?)?\s*;'
  gdscript:
    regexes: {}
```

内置默认配置等价于：

```yaml
classes_dir: doc/classes
sources:
  - glob: "**/*.h"
    language: cpp
  - glob: "**/*.hpp"
    language: cpp
  - glob: "**/*.gd"
    language: gdscript
```

## 注释书写约定

解析器识别文档注释，需遵循以下约定：

### C++（`CppParser`）

- 文档注释行以 `///` 或 `##` 开头；普通 `//`、`/*`、`*`、`#` 视为非文档注释，
  不会中断或触发提取。
- 类声明匹配 `class Name :` 或 `class Name {`（前向声明 `class X;` 不匹配）。
- 方法名通过第一个 `(` 前的最后一个标识符启发式提取；`GDVIRTUAL` 宏取括号内函数名。
- 成员变量匹配 `类型 成员名;` 或 `类型 成员名 = 默认;`。

```cpp
/// 字典工具类：提供字典结构相关的静态方法，不可实例化。
///
/// [param dict1] 第一个字典
/// [param dict2] 第二个字典
static bool has_same_keys_structure(Dictionary dict1, Dictionary dict2);

/// 设置路径（含点号分隔的层级名）
String setting_path = "";
```

### GDScript（`GDScriptParser`）

- 文档注释行以 `##` 开头；单个 `#` 为普通注释，不提取。
- 类名匹配 `class_name Foo`；变量匹配 `@export var name` 与 `var name`；
  方法匹配 `func name(`。

```gdscript
## 玩家基础控制类，处理移动与输入。
class_name Player

## 当前速度
@export var speed: float = 5.0

## 移动角色。[br][br]
## [param dir] 移动方向
func move(dir: Vector2) -> void:
	pass
```

### 通用规则

- 文档注释块必须紧邻其声明之前（注释 → 声明）。
- 同一逻辑元素只取声明前的最后一个注释块。
- 尚未对应的声明（如私有方法、不存在于 XML 中的成员）会被安全忽略。
- 行内标签 `[param]`、`[return]` 原样保留；`[br]` 在生成的 Markdown 中渲染为换行。

## 工作原理

1. `load_config` → 读取配置或使用内置默认。
2. `discover_sources` → 按 `sources`/`--headers` 匹配文件，路由到对应解析器。
3. `Parser.parse` → 逐行累积注释块，遇到类/方法/成员声明时分发到
   `DocClass`（类简介、方法字典、成员字典）。
4. `inject_doc` → 将描述注入 XML 的 `brief_description`、
   `<method>` 的 `<description>`、`<member>` 内文。
5. `match_class_xml` → 按 `{类名}.xml` 定位文件，内容有变化才写回。

内置解析器通过 `PARSERS` 注册表（扩展名 → 解析器类）路由。新增语言时继承
`ParserBase`（实现 `classify`、必要时覆写 `COMMENT_RE`/`NEUTRAL_RE`/`get_name`），
再注册到 `PARSERS` 即可，无需改动主流程。

## 集成到构建流程

项目内不再存放任何构建脚本。统一使用本仓库的一键构建工具 [`build.py`](build.py)，
所有路径由项目内配置 `docgen.yaml` 提供（以下值为仓库相对路径或占位符）：

```yaml
# docgen.yaml（位于接入本工具的模块项目内，通常放到 doc/ 下）
engine_bin_path: "<已编译含本模块的编辑器路径或所在目录>"
# project_root 可缺省：默认取当前工作目录（在模块项目根执行即可）
# project_root: "<模块项目根目录>"
classes_dir:   "doc/classes"
headers:       "**/*.h"
md_dir:        "doc/source/api"
# class.xsd 无需配置：build.py 会把工具集自带的 schema 复制到 <project_root>/doc/class.xsd，
# 并让生成的 XML 引用该项目内副本，不引用项目外文件。
```

`engine_bin_path` 可给**可执行文件完整路径**（如 `bin/godot.windows.editor.double.x86_64.console.exe`），
也可给**所在目录**（自动在 `editor`/`editor.double`/`dev`/`console` 各变体中选最新可用项）。`-g` 为最高优先级的覆盖。

```bash
# 一键全流程（doctool 生成 XML 骨架 + 注入注释 + 转 Markdown）
python build.py -c docgen.yaml -d -a

# 仅转 Markdown（XML 已就绪时）
python build.py -c docgen.yaml -a

# 仅 doctool + 注释注入；用 -g 可覆盖引擎二进制
python build.py -c docgen.yaml -d -g <godot_binary>
```

运行顺序：`doctool` 生成 XML 骨架 → `comment_doc_gen` 注入注释 → `xml_to_markdown`
转换 Markdown。`build.py` 内部按配置的 `headers` glob（逗号分隔多模式）调用
`comment_doc_gen`，再按 `md_dir` 输出 Markdown。`--doctool` 必须以独立参数传入
Godot 引擎，否则被当作位置参数而静默失败。