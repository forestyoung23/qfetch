---
name: single-movie-pilot
description: 根据一个或多个电影片名查找可用资源，检查容量、避免重复，操作夸克客户端下载并验收，生成Jellyfin命名和NFO。适用于Agent协助的macOS电影下载；不是无人值守下载守护进程。
---

# 电影下载与入库准备

用户提供片名后，执行“确认身份 → 查重 → 找源选版 → 容量登记 → 客户端下载 → 自动验收 → 入库准备”。多个片名按顺序执行同一流程；前一部尚在下载时保留队列，不同时启动多部。用户询问进展时读取记录，不重新创建任务。

## 开始执行

1. 将 `SKILL_DIR` 设为本文件所在目录。数据与技能分离，`MOVIE_HOME` 默认 `~/Documents/Codex/movie-library`；同一批任务始终使用同一数据目录。先检查用户是否已有配置和媒体库；存在时复用原数据目录，不初始化另一套空账本逃过查重。换机器默认重新初始化，迁移原文件和账本时按 [references/migration.md](references/migration.md) 执行。
2. 首次使用运行 `movie_manager.py --home "$MOVIE_HOME" init`；随后执行 `doctor`、`status`。数据目录可位于外接盘；不要在移动技能或换机器后直接使用历史绝对路径。依赖为Python 3、ffprobe、ffmpeg，下载路线另需夸克官方Skill、已登录客户端和实际可用的UI工具；不要求aria2作为夸克路线依赖。`doctor`只检查本地依赖，不代表账号、网络或UI已验证。
3. 确认目标目录仍在原磁盘，核实其他下载器的未完成任务和预算。未核实的外部预算不能默认为零。

```bash
python3 "$SKILL_DIR/movie_manager.py" --home "$MOVIE_HOME" init
python3 "$SKILL_DIR/movie_manager.py" --home "$MOVIE_HOME" doctor
python3 "$SKILL_DIR/movie_manager.py" --home "$MOVIE_HOME" status
```

## 身份与查重

通过实际搜索确认中文名、原名、年份及IMDb ID，区分同名影片和剪辑版；不根据下载文件名猜身份。先执行：

```bash
python3 "$SKILL_DIR/movie_manager.py" --home "$MOVIE_HOME" lookup --movie-id "imdb:tt..." --verify
```

- `already_verified`：重新核对散列与媒体流成功，返回现有路径，不再下载。展示现有版本；若用户明确要求升级或另一剪辑版，说明当前默认按影片ID去重，单独规划版本管理，不规避去重创建第二套账本。
- `existing_pending`：检查并恢复原任务，不重新转存或重复点击下载；客户端任务无法定位时报告需关注。
- `existing_needs_attention`：登记成片缺失或变化，先修复路径或分析变化；不静默替换或重下。
- `not_found`：继续找源。查询记录存在不等于本次内容重新验证成功。

## 找源与选择

优先使用当前用户可访问的夸克已有文件或公开分享，官方Skill负责搜索、分享详情与指定文件转存。调用前读取其当前 `SKILL.md`，遵循安装检查、授权、公共参数及返回提示规则；复用本机既有授权，不读取Cookie、复制登录配置或将授权码写入交付包。具体实测限制与步骤见 [references/quark.md](references/quark.md)。

必要时搜索公开资源页，核对真实文件大小、影片ID、内容版本、分辨率、片源、压制和字幕。优先在容量内选择画质较好的可用版本；不把文件大、编码新或导演剪辑版直接等同于更好。只有一个候选时如实说明比较范围。分享失效或接口失败时保留原因，有新依据再换来源，不机械重试。

默认直连；代理是否启用及费用由当前用户配置决定，不自动更改网络设置。只转存所选正片及确有需要的字幕，不转存整套合集。字幕来自其他发行版时不宣称同步。BT/直链备用路线见 [references/legacy-backends.md](references/legacy-backends.md)，不得把分享页当成直链填入aria2。

## 登记与下载

按 [references/manager.md](references/manager.md) 建立只含公开信息的候选清单。大小和验收范围必须已核实，不降低门槛让下载通过。登记前再查本地其他目录和客户端下载任务，不能只检查本账本。

```bash
python3 "$SKILL_DIR/movie_manager.py" --home "$MOVIE_HOME" prepare \
  --manifest "$MANIFEST" --other-reserved-bytes "$OTHER_RESERVED_BYTES"
```

登记在同一根锁内执行查重、容量预检和固定清单保存。默认预留两份正文的临时占用；只有已验证当前客户端写入方式、磁盘和临时目录后才能使用 `--temporary-copies 1`。目标盘保留至少50GiB或总容量10%的较大值，系统盘保留30GiB；暂停任务继续预留。容量不足不提交下载。

`awaiting_client`只表示预算已登记。通过当前可用的原生UI工具，在已登录夸克客户端中核对文件后，下载到返回的 `output_directory`。同片已有客户端任务时只恢复原任务。观察实际进度，再用 `observe --job "$JOB" --phase downloading` 记录；该命令不控制客户端。需要暂停时操作原任务，再记录 `paused`，不得影响其他任务。

UI不可用、主机锁屏或需要授权时，保留任务并准确说明需要用户完成的最小动作；不假装已经点击成功。客户端下载没有接入脚本自动暂停机制，下载中重新观察可用容量，低于保留线时通过UI暂停原任务。不承诺锁屏期间持续操控或永久后台监控。

## 完成与恢复

```bash
python3 "$SKILL_DIR/movie_manager.py" --home "$MOVIE_HOME" finish --job "$JOB" --wait-seconds 7200
```

使用可返回运行会话ID的后台执行工具启动等待并记录会话ID；同时推进独立工作，不能整轮阻塞等待几十分钟。无法后台执行时以 `finish --wait-seconds 0` 在后续查询，不虚构自动通知或定时续跑。客户端或Agent重新打开后，先 `status` 再恢复同一 `JOB`。

`finish`绑定原登记的清单、大小、路径和规格，只接受预期最终文件名；检查大小、媒体流、音轨、三处抽样解码和本地SHA256。成功后生成同盘硬链接和XML `movie.nfo`，登记完成并释放未来下载预算。失败保留文件与预算，不能报告已验证。硬链接不增加第二份视频，但修改任一路径内容会影响另一处。

取消任务时先通过UI确认原客户端下载已停止，再使用 `release --job "$JOB" --client-stopped --reason "$REASON"`；它只释放预算，不会停止客户端或删除文件。旧后端任务按其原工具处理，不手改账本释放预算。

## 最终结果

报告每部影片的状态、版本/实际规格、来源、字幕情况、成片路径及验收范围。区分下载中、下载完成待验收、验收通过和Jellyfin实际播放。入库准备不会自动修改Jellyfin服务器；用户要求导入时再核实服务器与目录访问。抽样解码和本地散列不能证明逐帧完好、字幕同步或发行方真实性。

不删除文件、购买会员或改代理。授权范围内的常规下载操作直接推进；不要因本技能流程本身反复请求确认。
