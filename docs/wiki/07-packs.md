# 07 包管理

[← 返回目录](README.md)

「⑧ 包管理」管理资源包（resource packs）与行为包（behavior packs）。

## 包列表

- 扫描服务器目录的 `resource_packs/` 与 `behavior_packs/`
- 显示包名、版本、启用状态
- 「刷新列表」重新扫描（手动往目录里放过文件后用）

## 启用 / 禁用

启用一个包会同时：

1. 写入当前活动世界的 `world_resource_packs.json` / `world_behavior_packs.json`
2. 登记到 `valid_known_packs.json`（BDS 用它识别可用包）

- **需要重启服务器生效**
- 禁用即从上述配置移除，包文件仍保留在磁盘

## 导入

支持 `.zip` 与 `.mcpack`：

1. 点「导入资源包 / 导入行为包」选择文件
2. 程序解压到对应目录
3. 新包默认**未启用**，需在列表中手动启用

导入校验：

| 校验 | 说明 |
|---|---|
| 结构 | ZIP 必须能打开，且至少包含一个 `manifest.json` |
| 同名冲突 | 已存在同名包时**拒绝导入**，需确认覆盖后带 `overwrite=true` 重试 |
| 落盘名 | Windows 会把非法字符替换为 `_`，守卫按**实际落盘名**判断，特殊字符名也无法绕过 |
| 解压体积 | 上限保护，防止 zip bomb |
| 路径 | 条目路径做边界校验，不能写出包目录 |

覆盖导入时会**先删旧目录**再解压，避免"新旧文件混合"（manifest 是新版、资源是并集）。

## 删除

- 点「删除」并输入 `DELETE` 确认
- 同时清理：包目录、`valid_known_packs.json` 条目、世界配置中的引用

## 常见问题

| 问题 | 原因与处理 |
|---|---|
| 导入后游戏里看不到 | 包默认未启用，请在列表启用并重启服务器 |
| 启用后不生效 | 确认修改的是**当前活动世界**的包配置；切世界后需重新启用 |
| 同名包导入失败 | 设计如此，避免静默覆盖；确认后重新导入并选择覆盖 |
| `.mcpack` 打不开 | 确认文件是 ZIP 容器（`.mcpack` 本质是 zip） |

## LeviLamina 插件（mods）

「⑧ 包管理」页底部提供 LeviLamina 支持（仅 Windows x64；LeviLamina 只发布 Windows 服务端包）。

### 启用 / 禁用加载器

1. 点「下载 lip」（从官方 GitHub Releases 下载并校验 SHA256）
2. 点「启用 LeviLamina」→ 确认后程序执行 `lip setup --game <版本> --build windows --loader levilamina`
3. 下载约 100 MB，完成后服务端变为 `bedrock_server_mod.exe`，程序会自动改用它启动
4. 「禁用」会把 `bedrock_server_mod.exe` 改名并恢复官方 `bedrock_server.exe`，`mods/` 与 lip 数据保留

> 启用会**按 LeviLamina 兼容版本重装服务端文件**（例如当前 LeviLamina 26.60.9 对应 BDS 1.26.60），
> 配置文件会先备份到 `mbes_backup/ll_<时间戳>/`。世界存档不受影响，但仍建议先做一次备份。

### 管理插件

| 操作 | 说明 |
|---|---|
| 安装插件包（.zip） | 上传 zip；lip 工作区优先走 `lip install`，否则安全解压到 `mods/<名>/` |
| lip 安装 / 卸载 | 填包名或 URL，执行 `lip install` / `lip uninstall` |
| 启用 / 禁用 | 在 `mods/` 与 `mods.disabled/` 之间移动 |
| 删除 | 删除插件目录/文件（需确认） |

- 插件名、版本、类型来自插件包里的 `manifest.json`
- `mods/LeviLamina` 是加载器本身，不会出现在插件列表里

## 相关

- 包会被更新覆盖：见 [01 安装与更新](01-install-update.md)（`resource_packs/` 属官方内容目录，随版本更新）
- 世界切换会改变"当前活动世界"，见 [05 世界管理](05-worlds.md)
