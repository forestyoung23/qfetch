# 统一入口

所有命令形如 `python3 "$SKILL_DIR/movie_manager.py" --home "$MOVIE_HOME" <command>`。数据路径固定后始终复用；技能目录可独立安装或更新。

| 命令 | 作用 |
|---|---|
| `init [--state-root EXISTING_ROOT]` | 初始化配置及独立下载、媒体库目录；已有配置原样复用。state-root用于接入已有账本，不复制媒体 |
| `doctor` | 检查ffprobe/ffmpeg、目录设备及本机夸克应用存在情况 |
| `status` | 汇总记录、待下载预算和当前可用容量；不证明进程仍活跃 |
| `lookup --movie-id imdb:tt... --verify` | 按影片ID查重，对已验收文件重新核对SHA256和媒体流 |
| `prepare --manifest FILE --other-reserved-bytes N` | 原子查重、预检容量、登记候选，返回指定下载目录；不启动客户端 |
| `observe --job DIR --phase downloading/paused` | 记录Agent现场观察到的状态，不修改实际进程 |
| `finish --job DIR [--wait-seconds 0..14400]` | 等待最终文件、验收、硬链接/NFO及完成登记 |
| `release --job DIR --client-stopped --reason TEXT` | 已确认客户端停止后释放预算，文件保留；不终止客户端 |

`prepare`允许的清单字段如下；阈值根据实际片源填写。本例只展示结构，不是可直接下载的资源：

```json
{
  "title": "影片名",
  "year": 2020,
  "movie_id": "imdb:tt0000001",
  "filename": "Example.2020.1080p.mkv",
  "size_bytes": 1000000000,
  "min_width": 1920,
  "min_height": 800,
  "min_duration_seconds": 7100,
  "max_duration_seconds": 7300,
  "source_page": "https://example.org/movie",
  "share_url": "https://pan.quark.cn/s/example",
  "version": "影院版"
}
```

不得填写未知大小、Cookie、FID、授权码、签名直链或凭证参数。默认临时空间按两份正文预留；观察并证实当前目录原地写入/改名后，可用 `--temporary-copies 1`。目标目录与系统盘不同时，默认还在系统盘预留一份正文临时空间及其他任务预算；不能只检查外接盘。已核实临时写入目标盘后才可采用单份模式。其他下载器预算必须显式传入，已查明无其他任务才用0。本账本暂停任务由脚本自动加入，不重复计入参数。

状态与退出码：缺少依赖、容量不足、需修复的成片、另一部客户端任务在进行时返回2；命令异常也返回2且输出JSON原因。`existing_pending`与`waiting_for_download`是有效的未完成状态，退出0不等于下载完成。CLI结果的status才决定下一步。

恢复时不编辑固定清单。若资源不可用，先停止原任务并释放预算；本版不会覆盖已有任务目录，不自动开启替代候选。停止并取消原任务后，可登记已核实的新候选；新清单生成独立目录，旧目录证据保留。同一份已取消清单不会被静默覆盖或重新提交，不能改低原阈值。当前版本升级/多剪辑版本同时保存需要单独规划，不是默认行为。

源码会检查配置目录的设备号。迁移到另一台机器或磁盘后新建数据配置，并验证媒体和账本迁移，不能照搬原设备号。跨盘硬链接不支持，不自动复制整部视频。
