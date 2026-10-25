# Movie Library Skill

给Agent一个电影片名，完成身份确认、资源选择、容量检查、下载验收和Jellyfin入库准备。多个片名依次处理，复用已有成片与任务，保留用户文件。

当前完整操作路线针对macOS：夸克官方Skill检查/转存，Agent操作夸克客户端下载，Python脚本登记和验收。需要Agent具备原生UI控制工具；单独安装CLI不会自动操作客户端。核心测试可以在Linux运行，Windows和Linux客户端自动化尚未支持。

## 安装

下载仓库ZIP并解压，或者克隆仓库：

```bash
git clone https://github.com/forestyoung23/movie-library-skill.git
cd movie-library-skill
```

在包含本README的目录执行：

```bash
python3 install.py
```

默认安装到 `$CODEX_HOME/skills/single-movie-pilot`，未设置CODEX_HOME时使用 `~/.codex/skills/single-movie-pilot`。已有相同版本会直接复用，已有不同版本会停止且不覆盖。其他Agent可使用：

```bash
python3 install.py --target /你的Agent技能目录/single-movie-pilot
```

然后在Agent中调用：

> 使用 $single-movie-pilot 下载《片名》，优先当前网络可访问的资源，执行容量检查、查重和成片验收。

如果宿主没有自动发现技能，可让Agent读取安装目录的SKILL.md。

## 每台机器的准备

- 安装Python 3.10及以上版本、ffmpeg和ffprobe；无需Python第三方包。ffmpeg/ffprobe优先从PATH查找，也兼容 `~/Tools/`。
- 安装夸克官方客户端和官方quarkclouddrive Skill，并在本机独立完成授权。本仓库不分发第三方客户端或官方Skill。
- 确认Agent可以读取客户端窗口并操作UI；锁屏或权限缺失时先恢复对应条件。
- 初始化自己的数据目录，不复制其他人的账号或旧机器配置。

```bash
python3 ~/.codex/skills/single-movie-pilot/movie_manager.py init
python3 ~/.codex/skills/single-movie-pilot/movie_manager.py doctor
```

以上示例使用默认安装目录；设置CODEX_HOME或自定义target时替换为实际路径。数据默认保存在 `~/Documents/Codex/movie-library`，与技能代码分离。自定义数据位置时，每条命令都传入同一home：

```bash
python3 /实际技能目录/movie_manager.py --home /Volumes/盘名/movie-library init
python3 /实际技能目录/movie_manager.py --home /Volumes/盘名/movie-library doctor
```

技能可以直接在新机器重新安装。已有媒体和未完成任务的迁移说明见 [migration.md](skills/single-movie-pilot/references/migration.md)。迁移代码不等于迁移客户端任务或授权。

## 更新

取得新版本代码后运行：

```bash
python3 install.py --upgrade
```

旧技能代码保存在技能目录之外的 `skill-backups`，数据目录不移动。若旧技能目录中夹有downloads、library等运行数据，安装器会停止，避免破坏旧下载任务。不要在下载或验收进程运行时替换其代码。

## 分享方式

现在可以直接分享本目录的ZIP，不需要附带对话记录。未来可把本目录作为GitHub仓库发布；其他人既能下载Release ZIP运行安装器，也能让支持GitHub技能安装的Agent安装 `skills/single-movie-pilot`。

仓库：https://github.com/forestyoung23/movie-library-skill 。可向支持GitHub技能安装的Agent发送“安装这个仓库中的 skills/single-movie-pilot”。

当前尚未指定开源许可证。维护者选定许可证后，才能明确允许他人使用、修改和再分发的范围；公开可见不等于已授予开源授权。

## 工作范围

- 按IMDb ID跨来源查重；现有文件重新核对SHA256和媒体流后复用。
- 目标盘保留至少50GiB或10%，系统盘至少30GiB；计入暂停任务和临时文件预算。
- 固定清单绑定大小、规格及路径；完成后核对媒体流、音轨、三处抽样解码和本地SHA256，生成同盘硬链接及XML NFO。
- 默认不改代理、不购买权益、不删除媒体。用户只应处理有权获取和保存的内容；仓库不提供资源库或影视文件。

原生客户端下载期间依赖Agent观察，脚本不会自动暂停客户端；没有Agent运行时不承诺自动检索、续跑或通知。Jellyfin目录准备与实际服务器导入/播放分开验收。候选可用性、字幕同步和客户端重启恢复需要各环境实际验证。

## 测试

```bash
python3 -m unittest discover -s skills/single-movie-pilot -p 'test_*.py' -v
python3 -m unittest test_install -v
```

完整媒体测试要求ffmpeg/ffprobe可用；缺失时会跳过相应测试，不能把跳过视为通过。公开目录仅包含代码、合成测试和通用说明，不包含个人影片、任务、账号、认证信息或历史会话记录。

本机验证：70项核心测试和8项安装/升级测试通过；仓库提供 `ci/github-actions-tests.yml` 工作流模板；当前上传凭据没有workflow权限，因此未启用远程自动测试。具有相应权限的维护者可将模板放入 `.github/workflows/tests.yml` 后启用。
