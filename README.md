# QFetch

[QFetch](https://github.com/forestyoung23/qfetch)（Quark Fetch）：让Agent通过夸克客户端下载单个文件到指定目录。

唯一下载路径为：**定位文件 → 检查原任务与空间 → 客户端下载 → 核对完成与落盘**。暂停与恢复使用客户端原任务。项目不包含网页下载器、备用路线、批量队列或Jellyfin操作。

## 使用条件

- 已登录的夸克客户端，当前验证macOS夸克7.4.2.1020、内置网盘2.5.40。
- 能观察和操作原生窗口的Agent；可使用辅助功能、截图或键鼠，不绑定某个Agent的工具API。
- Python 3.10以上，无第三方包。`download.py`只检查本地任务和文件，不启动下载。
- 搜索、元数据读取和分享转存优先使用官方`quarkclouddrive`技能；未安装时从可信官方来源自动安装。不可用时使用客户端，实际下载始终走客户端。

按可靠性、可用性与操作成本选择文件操作方式，已有结果或任务直接复用。安装与授权不可用不阻断已登录客户端的使用；[官方技能准备](skills/qfetch/references/official-skill.md)给出发现、安装及交接步骤。

技能入口给出完成标准，[客户端操作流程](skills/qfetch/references/client-workflow.md)固定已验证的搜索、下载、目录选择、暂停及恢复路径。客户端升级时以当前控件含义和实际结果适配，避免盲用旧坐标。

## 安装

```bash
python3 install.py
python3 install.py --upgrade
```

默认安装到`$CODEX_HOME/skills/qfetch`，未设置时为`~/.codex/skills/qfetch`。其他Agent可指定其技能目录：

```bash
python3 install.py --target /实际Agent技能目录/qfetch
```

安装器复制完整技能目录；更新先备份旧代码。下载数据放在技能目录之外，含任务记录的安装目录不会被自动升级。旧客户端任务继续在原客户端处理；旧网页脚本的`.part`和记录保留，不自动迁移。

## 使用

向Agent提出：

> 使用 qfetch，将这个夸克分享中的指定文件下载到 /我的下载目录，完成后告诉我保存路径。

每个文件使用独立的已存在目录。Agent先核对来源、完整文件名及客户端显示的大小，检查客户端是否已有任务，再运行：

```bash
python3 skills/qfetch/download.py prepare --directory /下载目录 --filename example.mkv --size 1000000000 --source 账号别名:/网盘路径/example.mkv
python3 skills/qfetch/download.py status --directory /下载目录
```

`ready`仅表示本地检查通过；`existing_task`仅表示本地登记已存在，并不证明客户端任务已创建。`.qfetch.json`记录来源、文件名和大小，重复请求复用；原任务存在时只恢复原任务。已知精确大小时，脚本检查目标盘完整文件大小加64MiB空余；仅有客户端约数时省略`--size`，脚本返回可用空间，Agent需按显示大小保守估算余量。不把临时文件大小当作进度；其他缓存盘还需结合客户端实际设置检查。

只有客户端“已完成”中核对了同一文件和保存目录，才执行：

```bash
python3 skills/qfetch/download.py check --directory /下载目录 --client-completed
```

`complete`表示客户端完成、最终文件已落盘且无残留下载临时文件；`size_verified=true`才表示精确大小也已核对。未知大小时报告实际字节数及`size_verified=false`，不表示内容校验和或媒体播放已验证。已有文件不会被脚本覆盖或删除。

## 实测与边界

2026-10-26，同一账号、同一4,164,241,147字节（3.88GiB）文件：客户端下载显示41.6–42.9MB/s，连续40秒落盘采样约40.99MiB/s；在73.9%暂停后恢复，最终完整落盘，大小正确。前65MiB与先前网页下载数据一致，已记录本地SHA-256；没有云端整文件校验和可供比对。

客户端路径已验证一次GB级文件下载与普通暂停恢复，未验证客户端重启后的续传或其他操作系统。后续排查发现，本机全屏窗口曾出现“可读取但无法输入”的问题；退出全屏后恢复。对“前往文件夹”重新获取模态窗口、直接设置路径后，已自动完成一次1,048,183字节小文件的目录选择、下载和落盘核对，全程无需人工点击。流程默认普通窗口，固定恢复顺序，失败后报告具体环境阻碍。此结果尚不代表长期无人值守、所有Agent或其他显示器配置均已验收。

## 验证

```bash
python3 -m unittest discover -s skills/qfetch -p 'test_*.py' -v
python3 -m unittest test_install -v
```

测试覆盖本地检查和安装，不代表UI自动化验收。`ci/github-actions-tests.yml`仅为参考模板，尚未启用GitHub Actions。

仓库只包含代码、测试和通用说明，不分发资源或账号凭据。当前尚未指定开源许可证。
