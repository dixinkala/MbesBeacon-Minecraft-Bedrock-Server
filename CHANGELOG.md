# Changelog

**Languages: 中文 | [English](CHANGELOG_EN.md)**

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)（SemVer）。版本号不采用 Minecraft Java 版风格命名规则。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.0.23] - 2026-09

### ✨ 新增

- 软件更新弹窗修复：检测到新版本时弹窗现在会正常显示（此前弹窗被创建但不可见）
- 更新说明文本经 HTML 转义，防止特殊字符导致弹窗异常
- 项目文档补充英文版（README/SECURITY/CONTRIBUTING/CODE_OF_CONDUCT/PR 与 Issue 模板）
- README 顶部增加界面预览截图与 CI/CodeQL/测试数徽章
- 仓库 Topics 补充至 14 个关键词（新增 `bedrock-dedicated-server`、`game-server`）

### 🔧 修改

- EXE 包名规范为「软件名 + 版本号」（`MbesBeacon1.0.23.exe`），版本元数据同步
- 移除语言切换/国际化相关代码，界面统一使用内置中文

### 🐛 修复

- 修复主题保存功能崩溃（`DEFAULT_THEME` 未定义）
- 修复备份删除接口路径校验可被绕过
- 修复 IP 封禁接口未校验 IP 格式
- 修复健康检查端点因缺少 `psutil` 依赖而崩溃
- 修复 `/api/config` GET 端点重复发送 HTTP 响应
- 修复性能监控中玩家数恒为 0、CPU 占用硬编码为 0
- 修复崩溃自动重启无限循环
- 修复下载完整性校验不阻断安装、SHA256 校验实际无效
- 修复备份恢复/删除无路径校验
- 修复 `ignore_ssl` 选项可被 API 任意设置
- 修复单实例互斥量残留问题
- 修复下载哈希本地缓存永久失效（`register_known_hash` 重复定义覆盖）

### 🏗️ 架构与性能

- 全面使用 `AppContext` 依赖注入，消除模块级全局变量
- 拆分 `do_GET`/`do_POST` 巨型方法为路由表注册模式
- 拆分单文件为多模块包结构（config/console/utils/players/backup/server/install/web）
- 新增 API 速率限制、健康检查端点
- 新增审计日志、危险操作二次确认
- 新增配置项数值范围校验、自定义下载 URL 安全校验
- 新增崩溃日志环境信息
- 修复 `extract_zip` 路径遍历检测逻辑缺陷
- 缓存 `detect_servers()` 结果，消除高频磁盘扫描

---

## [1.0.22] - 2026-09

### ✨ 新增

- 添加自动/手动检查软件更新机制（与服务器版本更新区分）
- 语言设定跟随系统语言（首次启动检测一次）
- 添加切换语言按钮、导入/导出存档
- 添加安全政策（SECURITY.md）、英文文档

### 🔧 修改

- 移除语言切换/国际化相关代码（后续版本）
- 版本号改回 1.0.x 命名（不使用 MC Java 风格）

### 🐛 修复

- 修复 UI 交互异常、托盘右键菜单空白
- 修复启动后停留在加载界面
- 修复联网搜索版本列表仅剩一个版本
- 修复删除服务器后左上角目录仍显示路径
- 修复检查更新检测不到当前安装版本
- 修复退出按钮未正常关闭软件

---

## [1.0.21] - 2026-09

### ✨ 新增

- 首个 GitHub Release 发布（v1.0.21）
- README 完善（项目说明、主要功能、安装方法、使用方法、输入输出示例）
- 系统托盘支持、多种界面风格切换
- 计划任务模块（定时重启/备份/公告）
- 服务器性能监控面板
- 下载完整性校验、危险操作确认
- 多世界管理界面、开机自启开关、命令自动补全

### 🏗️ 架构与性能

- 拆分单文件为多模块包结构
- 新增 API 速率限制
- 新增配置项数值范围校验

---

## [1.0.20] 及更早版本

早期迭代版本，包含：服务器自动安装（官方源+多下载源）、玩家管理（权限/踢出/封禁/白名单）、多服务器检测与管理、世界备份、配置可视化编辑与回滚、SSE 实时控制台、主题系统、检查更新机制等基础功能。

[Unreleased]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/compare/v1.0.23...main
[1.0.23]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases/tag/v1.0.23
[1.0.22]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases/tag/v1.0.22
[1.0.21]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases/tag/v1.0.21
