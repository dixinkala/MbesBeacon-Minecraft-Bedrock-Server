# Contributing Guide

First of all, thank you for taking the time to contribute to MbesBeacon! 🎉

We welcome any form of contribution — bug reports, feature suggestions, or direct code submissions.

**Languages: [中文](CONTRIBUTING.md) | English**

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How to Contribute](#how-to-contribute)
- [Setting Up the Dev Environment](#setting-up-the-dev-environment)
- [Code Standards](#code-standards)
- [Commit Message Convention](#commit-message-convention)
- [Reporting Bugs](#reporting-bugs)
- [Feature Suggestions](#feature-suggestions)
- [Pull Request Workflow](#pull-request-workflow)

---

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md) code of conduct. By participating, you agree to abide by its terms.

---

## How to Contribute

### 1. Report a Bug

If you found a bug, please file an [Issue](https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/issues/new?template=bug_report.md) and include:

- OS version (Windows 10/11)
- MbesBeacon version
- Reproduction steps
- Expected vs. actual behavior
- Screenshots/logs (if any)

### 2. Suggest a Feature

If you have an idea for a new feature, please file an [Issue](https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/issues/new?template=feature_request.md) and describe:

- The feature itself
- The problem it solves
- Expected implementation approach (optional)

### 3. Submit Code

If you want to contribute code directly, please follow the [Pull Request Workflow](#pull-request-workflow).

---

## Setting Up the Dev Environment

### Prerequisites

- Python 3.10+
- Windows 10/11 (this project supports Windows only)
- Git

### Setup Steps

```bash
# 1. Fork and clone the project
git clone https://github.com/<your-username>/MbesBeacon-Minecraft-Bedrock-Server.git
cd MbesBeacon-Minecraft-Bedrock-Server

# 2. Create a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt  # dev dependencies (if created)

# 4. Run the dev version
python run.py

# 5. Run tests
pytest tests/ -v

# 6. Format code
ruff format .

# 7. Lint
ruff check .
```

### Building the EXE

```bash
# Install PyInstaller
pip install pyinstaller

# Build
python -m PyInstaller --noconfirm --clean BedrockServerManager.spec

# Build output at dist/MbesBeacon.exe
```

---

## Code Standards

### Python Code

- Follow [PEP 8](https://peps.python.org/pep-0008/) style
- Use `ruff` for formatting and linting
- Functions and methods must have docstrings
- Type annotations are required
- All new features must include unit tests

### Front-end Code

- HTML/CSS/JS follows general best practices
- Use semantic HTML tags
- CSS class names use kebab-case
- JS variables use camelCase

### Pre-commit Checks

Before committing code, make sure:

```bash
# 1. Format
ruff format .

# 2. Lint passes
ruff check .

# 3. All tests pass
pytest tests/ -v

# 4. Build test (optional)
python -m PyInstaller --noconfirm --clean BedrockServerManager.spec
```

---

## Commit Message Convention

This project follows the [Conventional Commits](https://www.conventionalcommits.org/) spec.

### Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

| Type | Description |
|------|------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation update |
| `style` | Code formatting (no functional change) |
| `refactor` | Code refactoring |
| `perf` | Performance improvement |
| `test` | Test-related |
| `chore` | Build/tooling/dependency updates |
| `ci` | CI/CD related |

### Examples

```
feat(player): add IP ban support

- Add /api/ipban/add and /api/ipban/remove endpoints
- Add IP ban management UI
- Add unit tests

Closes #123
```

```
fix(config): fix max-players range validation

- Change max-players range from (1, 99999) to (1, 100)
- Add boundary value tests

Fixes #456
```

---

## Reporting Bugs

When filing a bug report, describe the problem in as much detail as possible to help us locate and fix it quickly.

### Required Information

- [ ] OS version (e.g., Windows 10 22H2 / Windows 11 23H2)
- [ ] MbesBeacon version (e.g., v1.0.21)
- [ ] Reproduction steps (down to each operation)
- [ ] Expected behavior
- [ ] Actual behavior

### Optional Information

- [ ] Screenshot or screen recording
- [ ] Log files (`%APPDATA%\MbesBeacon\logs\`)
- [ ] Crash report (`%APPDATA%\MbesBeacon\crash.log`)
- [ ] Server config file (`server.properties`; redact any sensitive info)

---

## Feature Suggestions

When suggesting a feature, please explain:

### Required Information

- [ ] Feature description (what it does)
- [ ] Problem solved (why it is needed)
- [ ] Use case (when it would be used)

### Optional Information

- [ ] Expected implementation approach
- [ ] Reference implementations (similar features in other software)
- [ ] UI/UX design sketches
- [ ] Related Issue or discussion links

---

## Pull Request Workflow

### 1. Create a Branch

Create a feature branch from `main`:

```bash
git checkout main
git pull origin main
git checkout -b feature/your-feature-name
```

Branch naming conventions:
- `feature/xxx`: new feature
- `fix/xxx`: bug fix
- `docs/xxx`: documentation update
- `refactor/xxx`: code refactoring
- `perf/xxx`: performance improvement

### 2. Commit Changes

```bash
git add .
git commit -m "feat: add new feature"
```

### 3. Push to Your Fork

```bash
git push origin feature/your-feature-name
```

### 4. Create a Pull Request

Create a Pull Request on GitHub containing:

- **Title**: A concise description (following Conventional Commits)
- **Description**:
  - What and why you changed
  - Related Issues (e.g., `Closes #123`)
  - Test results
  - Screenshots/screen recordings (for UI changes)
- **Checklist**:
  - [ ] Code follows project standards
  - [ ] Unit tests added
  - [ ] All tests pass
  - [ ] Documentation updated (if needed)

### 5. Code Review

- Maintainers will review your code
- Changes may be requested
- Please respond to review comments promptly

### 6. Merge

- Once approved, a maintainer will merge your PR
- Congratulations, you're now a contributor to this project! 🎉

---

## FAQ

### Q: Can I submit only documentation updates?

A: Of course! Documentation improvements are a very valuable contribution.

### Q: I'm a beginner, can I contribute code?

A: Absolutely welcome! We tag some `good first issue`s for newcomers to get started.

### Q: Is there a reward for contributing?

A: This is an open-source free project with no financial reward. But your contribution will be recorded in the contributor list and appreciated by the community!

### Q: Who do I ask questions?

A: File an Issue, or ask in Discussions.

---

## Thanks

Thank you again for your contribution! Every contribution makes this project better. ❤️

If you like this project, please give it a Star ⭐ to support it!
