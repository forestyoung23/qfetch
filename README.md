# QFetch

[QFetch](https://github.com/forestyoung23/qfetch)（Quark Fetch）让Agent通过Google发现资源、核验夸克分享，再使用夸克客户端下载并核对本地文件。支持单文件及资源清单。Python 3.10以上，无第三方包；需要可观察、可操作的macOS夸克客户端。

下载边界及阶段入口统一维护于[SKILL.md](skills/qfetch/SKILL.md)。脚本只负责候选选择与本地检查，不控制UI或传输。官方`quarkclouddrive`用于结构化检索、元数据和转存，安装及授权见[官方技能准备](skills/qfetch/references/official-skill.md)。

## 安装与检查

```bash
python3 install.py
python3 install.py --upgrade
python3 install.py --check
```

默认安装到`$CODEX_HOME/skills/qfetch`，未设置时为`~/.codex/skills/qfetch`；其他Agent可指定`--target /实际技能目录/qfetch`。升级备份旧代码，含任务记录的安装目录不会自动升级。下载数据应放在技能目录之外。

`--check`只读比较仓库与安装目录，输出SHA-256内容指纹及缺失、多余、变更文件；一致时退出码为0，不一致或未安装为1。升级后运行该检查确认内容一致。

## 使用

> 使用 qfetch，将《影片A》（2025）和《影片B》（2026）下载到 /我的下载目录，完成后汇总来源和保存路径。

也可直接提供夸克分享链接。条件型榜单请求先明确作品清单、榜单来源和口径。

多项请求采用[两项流水执行](skills/qfetch/references/batch-workflow.md)：一个文件下载期间检索下一项，最多保留一个预备项；前项完成核对后才提交后项，客户端操作串行。空间预留、异常处理及交接规则集中维护于该文档。

按需参考：

- [Google检索与预算](skills/qfetch/references/web-search.md)：检索入口、超时恢复与候选核验。
- [资源选择](skills/qfetch/references/resource-selection.md)：分辨率、体积、MKV偏好与`select_resource.py`输入。
- [客户端流程](skills/qfetch/references/client-workflow.md)：能力自检、窗口恢复、下载操作及停滞判定。
- [本地检查](skills/qfetch/references/local-checks.md)：`prepare`、`status`、`check`及完成标准。

## 验证与边界

```bash
python3 -m unittest discover -s skills/qfetch -p 'test_*.py' -v
python3 -m unittest test_install -v
```

测试覆盖本地选择、任务检查和安装，不代表UI自动化验收。`ci/github-actions-tests.yml`仅为参考模板，尚未启用GitHub Actions。

历史实测环境为macOS夸克7.4.2.1020、内置网盘2.5.40。2026-10-26已验证GB级单文件下载、普通暂停恢复和精确大小核对；其中4,324,323,426字节文件还通过媒体元数据读取与开头、中段、结尾各5秒的视频及双音轨解码。没有云端整文件校验和对照，未做全片解码。相关窗口故障与恢复经验见客户端流程。

两项流水及新停滞阈值需另行实测，不以历史单文件成功作为验收依据。客户端重启续传、其他操作系统及长期无人值守尚未验证。仓库不分发资源或账号凭据，当前尚未指定开源许可证。
