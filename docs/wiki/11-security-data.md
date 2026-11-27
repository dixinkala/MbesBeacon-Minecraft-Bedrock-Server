# 11 安全与数据目录

[← 返回目录](README.md)

## 安全机制

| 机制 | 说明 |
|---|---|
| API Token | 本地 Web API 需带 `X-API-Token`；比较使用常量时间算法 |
| Host 头校验 | 拒绝非本机 Host 的请求，缓解 DNS rebinding |
| 仅本机可用 | 默认只监听本机，跨机访问需自行放宽（不建议暴露公网） |
| 路径边界 | 删除/移动/恢复/导入等操作统一校验目标必须位于服务器目录内 |
| 命令校验 | 拒绝控制字符；危险命令先归一化再判定，防止 `/stop` 之类绕过 |
| 下载校验 | 大小、ZIP 结构、版本号官方清单、TOFU SHA256、PE 签名 |
| 签名策略 | 篡改（摘要不匹配、伪造证书）无条件失败；未签名默认告警，`MBES_REQUIRE_SIGNATURE=1` 可强制 |
| 审计日志 | 关键操作留痕，按大小轮转 |
| 原子写入 | 配置/权限/白名单等写入采用临时文件 + 原子替换，崩溃不产生半截文件 |

> MbesBeacon 完全本地运行，不上传任何数据。

## 数据目录

用户数据统一存放在 `%APPDATA%\MbesBeacon\`：

```
%APPDATA%\MbesBeacon\
├─ settings.json                应用设置（服务器目录、已安装版本、语言等）
├─ theme.json                   主题配置
├─ settings 历史/配置历史        配置回滚用的快照
├─ logs\
│  ├─ app.log                   应用日志
│  ├─ crash.log                 崩溃日志
│  └─ audit.log                 审计日志（按大小轮转，如 audit.1.log）
└─ cache\
   ├─ hashes.json               TOFU 哈希清单
   ├─ hashes.json.mac           清单的 HMAC 签名
   ├─ app_update_cache.json     软件更新检查缓存（1 小时）
   └─ bds_versions.json         官方版本清单缓存
```

服务器目录下的文件（属于该服务器）：

```
<服务器目录>\
├─ bedrock_server.exe           服务端可执行文件
├─ server.properties            配置
├─ permissions.json             权限
├─ allowlist.json               白名单
├─ banned-players.json          封禁玩家
├─ banned-ips.json              IP 封禁
├─ worlds\                      世界存档
├─ resource_packs\              资源包
├─ behavior_packs\              行为包
├─ .install_manifest.json       安装清单（用于安全清理旧文件）
└─ .install_staging\            下载/解压暂存（安装时会短暂存在）
```

备份目录：服务器目录同级的 `_worlds_backups\`。

## 备份与迁移

- 换机器：复制 `%APPDATA%\MbesBeacon\` + 服务器目录（含 `worlds/`）
- 只迁存档：用「⑤ 世界管理」导出 ZIP，或直接拷 `worlds/`
- 迁完首次启动会重新检测服务器目录，必要时用「📁 浏览」重新指定

## 日志与排错

| 文件 | 用途 |
|---|---|
| `logs\app.log` | 日常运行日志 |
| `logs\crash.log` | 崩溃堆栈 |
| `logs\audit.log` | 谁在什么时候做了什么 |
| 控制台「导出日志」 | 当前会话输出 |

## 安全建议

1. 不要把管理界面暴露到公网（默认仅本机）
2. 从官方渠道下载服务端；自定义 URL 只用于可信镜像
3. 开启 `online-mode` 或至少配置白名单
4. 定期备份 `worlds/` 与 `%APPDATA%\MbesBeacon`
5. 关注 Release 页面获取安全更新
