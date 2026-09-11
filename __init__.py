# coding: utf-8
"""godot_docs — 跨项目通用的 Godot 模块文档生成工具集。

将源码注释与类 XML 骨架转换为 Godot 风格 Markdown API 文档。模块间相互独立，
路径全部由调用方配置提供，不依赖脚本自身位置，可复用于任意 Godot 模块项目。

| 模块           | 职责                                         |
| -------------- | -------------------------------------------- |
| `comment_doc_gen` | 源码文档注释 → 注入 Godot 类 XML 骨架        |
| `xml_to_markdown` | Godot 类 XML → Markdown API 文档           |
| `bbcode_to_markdown` | Godot 文档 BBCode → Markdown（依赖下两者）  |
| `bbcode`          | 通用 BBCode 解析器                          |
| `markdown`        | 通用 Markdown 输出辅助                      |
| `build`           | 一键编排：doctool + comment_doc_gen + xml→markdown |
"""