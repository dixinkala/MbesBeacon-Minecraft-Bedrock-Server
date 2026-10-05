# 贡献指南

**Languages: 中文 | [English](CONTRIBUTING_EN.md)**

首先，感谢你花时间为 MbesBeacon 做贡献！🎉

我们欢迎任何形式的贡献，无论是提交 Bug 报告、功能建议，还是直接提交代码。

## 目录

- [行为准则](#行为准则)
- [如何贡献](#如何贡献)
- [开发环境搭建](#开发环境搭建)
- [代码规范](#代码规范)
- [提交信息规范](#提交信息规范)
- [报告 Bug](#报告-bug)
- [功能建议](#功能建议)
- [Pull Request 流程](#pull-request-流程)

---

## 行为准则

本项目采用 [Contributor Covenant](CODE_OF_CONDUCT.md) 行为准则。参与项目即表示你同意遵守该准则。

---

## 如何贡献

### 1. 报告 Bug

如果你发现了 Bug，请提交 [Issue](https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/issues/new?template=bug_report.md)，并包含以下信息：

- 操作系统版本（Windows 10/11）
- MbesBeacon 版本号
- 复现步骤
- 预期行为 vs 实际行为
- 截图/日志（如有）

### 2. 功能建议

如果你有新功能的想法，请提交 [Issue](https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/issues/new?template=feature_request.md)，并说明：

- 功能描述
- 解决的问题
- 预期的实现方式（可选）

### 3. 提交代码

如果你想直接贡献代码，请遵循 [Pull Request 流程](#pull-request-流程)。

---

## 开发环境搭建

### 前置要求

- Python 3.10+
- Windows 10/11（本项目仅支持 Windows 平台）
- Git

### 安装步骤

```bash
# 1. Fork 并克隆项目
git clone https://github.com/<your-username>/MbesBeacon-Minecraft-Bedrock-Server.git
cd MbesBeacon-Minecraft-Bedrock-Server

# 2. 创建虚拟环境（推荐）
python -m venv venv
venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt  # 开发依赖（如已创建）

# 4. 运行开发版本
python run.py

# 5. 运行测试
pytest tests/ -v

# 6. 代码格式化
ruff format .

# 7. 代码检查
ruff check .
```

### 打包 EXE

```bash
# 安装 PyInstaller
pip install pyinstaller

# 打包
python -m PyInstaller --noconfirm --clean BedrockServerManager.spec

# 打包产物在 dist/MbesBeacon.exe
```

---

## 代码规范

### Python 代码

- 遵循 [PEP 8](https://peps.python.org/pep-0008/) 代码风格
- 使用 `ruff` 进行代码格式化和检查
- 函数和方法必须有 docstring
- 类型注解是必须的
- 所有新功能必须包含单元测试

### 前端代码

- HTML/CSS/JS 遵循通用最佳实践
- 使用语义化 HTML 标签
- CSS 类名使用 kebab-case
- JS 变量使用 camelCase

### 提交前检查

提交代码前，请确保：

```bash
# 1. 代码格式化
ruff format .

# 2. 代码检查通过
ruff check .

# 3. 所有测试通过
pytest tests/ -v

# 4. 打包测试（可选）
python -m PyInstaller --noconfirm --clean BedrockServerManager.spec
```

---

## 提交信息规范

本项目遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范。

### 格式

```
<type>(<scope>): <subject>

<body>

<footer>
```

### 类型（type）

| 类型 | 说明 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 文档更新 |
| `style` | 代码格式（不影响功能） |
| `refactor` | 代码重构 |
| `perf` | 性能优化 |
| `test` | 测试相关 |
| `chore` | 构建/工具/依赖更新 |
| `ci` | CI/CD 相关 |

### 示例

```
feat(player): 添加 IP 封禁功能

- 新增 /api/ipban/add 和 /api/ipban/remove 端点
- 新增 IP 封禁管理 UI
- 添加单元测试

Closes #123
```

```
fix(config): 修复 max-players 范围校验失效

- 将 max-players 范围从 (1, 99999) 改为 (1, 100)
- 添加边界值测试

Fixes #456
```

---

## 报告 Bug

提交 Bug 报告时，请尽可能详细地描述问题，帮助我们快速定位和修复。

### 必含信息

- [ ] 操作系统版本（Windows 10 22H2 / Windows 11 23H2 等）
- [ ] MbesBeacon 版本号（v1.0.21 等）
- [ ] 复现步骤（详细到每一步操作）
- [ ] 预期行为
- [ ] 实际行为

### 可选信息

- [ ] 截图或录屏
- [ ] 日志文件（`%APPDATA%\MbesBeacon\logs\`）
- [ ] 崩溃报告（`%APPDATA%\MbesBeacon\crash.log`）
- [ ] 服务器配置文件（`server.properties`，如有敏感信息请脱敏）

---

## 功能建议

提交功能建议时，请说明：

### 必含信息

- [ ] 功能描述（这个功能是什么）
- [ ] 解决的问题（为什么需要这个功能）
- [ ] 使用场景（在什么情况下使用）

### 可选信息

- [ ] 预期的实现方式
- [ ] 参考实现（其他软件的类似功能）
- [ ] UI/UX 设计草图
- [ ] 相关的 Issue 或讨论链接

---

## Pull Request 流程

### 1. 创建分支

从 `main` 分支创建功能分支：

```bash
git checkout main
git pull origin main
git checkout -b feature/your-feature-name
```

分支命名规范：
- `feature/xxx`：新功能
- `fix/xxx`：Bug 修复
- `docs/xxx`：文档更新
- `refactor/xxx`：代码重构
- `perf/xxx`：性能优化

### 2. 提交更改

```bash
git add .
git commit -m "feat: 添加新功能"
```

### 3. 推送到 Fork

```bash
git push origin feature/your-feature-name
```

### 4. 创建 Pull Request

在 GitHub 上创建 Pull Request，包含：

- **标题**：简洁明了的描述（遵循 Conventional Commits 规范）
- **描述**：
  - 更改的内容和原因
  - 关联的 Issue（如 `Closes #123`）
  - 测试结果
  - 截图/录屏（如有 UI 更改）
- **检查清单**：
  - [ ] 代码遵循项目规范
  - [ ] 已添加单元测试
  - [ ] 所有测试通过
  - [ ] 已更新文档（如需要）

### 5. 代码审查

- 维护者会审查你的代码
- 可能会要求一些修改
- 请及时回复审查意见

### 6. 合并

- 审查通过后，维护者会合并你的 PR
- 恭喜！你成为了本项目的贡献者！🎉

---

## 常见问题

### Q: 我可以只提交文档更新吗？

A: 当然可以！文档改进也是非常有价值的贡献。

### Q: 我是新手，可以贡献代码吗？

A: 当然欢迎！我们会标注一些 `good first issue`，适合新手入手。

### Q: 贡献代码有报酬吗？

A: 本项目是开源免费项目，没有经济报酬。但你的贡献会被记录在贡献者名单中，并且会获得社区的感谢！

### Q: 我有问题可以问谁？

A: 可以提交 Issue，或者在 Discussion 中提问。

---

## 感谢

再次感谢你的贡献！每一个贡献都让这个项目变得更好。❤️

如果你喜欢这个项目，请给个 Star ⭐ 支持一下！
