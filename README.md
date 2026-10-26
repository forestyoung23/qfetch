# QFetch

仓库：[forestyoung23/qfetch](https://github.com/forestyoung23/qfetch)。

QFetch（Quark Fetch），让Agent通过夸克下载文件：定位文件 → 检查重复任务和空间 → 操作客户端下载 → 确认文件落盘。

当前支持macOS夸克客户端，需要Agent具备原生UI操作能力。Python脚本只做本地检查，不控制下载。

2026-10-26实测官方Skill/CLI 1.0.25：当前账号可直接下载1,048,183字节的网盘使用指南图片；下载55,539,474字节的视频时返回`23018: download file size limit[52428800]`，未生成本地文件。因此当前仍采用客户端下载大文件；该结果不代表所有账号均有相同限制，断点续传尚未验证。

## 安装与使用

准备Python 3.10以上版本、夸克官方客户端及官方`quarkclouddrive`技能，并在本机完成授权。无需Python第三方包。

在仓库目录执行：

```bash
python3 install.py
```

默认安装到`$CODEX_HOME/skills/qfetch`，未设置CODEX_HOME时使用`~/.codex/skills/qfetch`。其他Agent可以通过`--target /实际技能目录/qfetch`指定位置。

然后向Agent提出：

> 使用 $qfetch，将这个夸克分享中的指定文件下载到 /我的下载目录，完成后告诉我保存路径。

也可以提供片名或文件名，由Agent先在夸克中定位文件。每个文件使用独立下载目录，不重复创建已有客户端下载任务。

## 本地检查

```bash
python3 skills/qfetch/download.py prepare --directory /已存在的下载目录 --filename example.mp4 --size 1000000000
python3 skills/qfetch/download.py status --directory /已存在的下载目录
python3 skills/qfetch/download.py check --directory /已存在的下载目录 --client-completed
```

`prepare`检查空间并保存文件名和大小，不启动下载。客户端实际显示完成后，才可传入`--client-completed`；脚本核对最终文件是否存在且大小一致。这些检查不证明内容完整或可播放。

空间规则为两倍文件大小加5GiB；外接盘下载时也检查系统盘。每个目录只有一条`.quark-download.json`记录，重复请求复用原记录；另一文件使用另一目录。脚本不覆盖或删除下载文件，也不会自动暂停客户端。

## 更新与测试

```bash
python3 install.py --upgrade
python3 -m unittest discover -s skills/qfetch -p 'test_*.py' -v
python3 -m unittest test_install -v
```

更新会备份旧技能代码。不要在相关进程运行时替换代码，下载文件应保存在技能目录之外。旧版用户安装新技能后停用旧技能，已有客户端下载任务继续在原客户端处理；本项目不迁移或修改历史任务数据。

仓库内只有代码、测试和通用说明。不分发文件资源或账号信息；仅获取有权下载的内容。当前尚未指定开源许可证。
