# 备用后端

默认采用夸克客户端，由movie_manager.py统一登记及验收。以下脚本保留为可选能力；它们不是统一入口的自动后端，执行前仍需查重并核对同一影片没有另一下载器正在运行。

| 工具 | 用途和约束 |
|---|---|
| discover.py | WebTorrent开放目录查询及v1磁力元数据预检，不是全网搜索器 |
| forum_source.py | 指定1LOU公开资源帖的IMDb核对和种子取得，不负责搜索网站 |
| acquire.py | HTTPS/单视频ZIP/固定种子下载，需aria2c；运行前检查清单、依赖及容量 |
| transmission_pilot.py | 隔离Transmission RPC试验后端，需单独安装对应命令行工具 |
| verify_torrent_file.py | 已固定种子的所选文件离线分片核对；跨文件边界范围单独说明 |
| register_external.py | 固定种子的外部完成文件验收与硬链接入库，需原任务账本 |
| report_status.py | 读取旧任务状态，存储的速度不证明当前进程仍运行 |
| cancel_job.py | 停止并取消其支持的旧任务；不用于控制Quark客户端 |

执行前读取对应命令的 `--help` 和清单验证逻辑，不能将登录态网页、网盘分享或签名直链硬塞进acquire.py。传输依赖及实际网络条件均需本机验证；保留暂停任务预算，不并行提交同片不同后端。

没有固定BT种子的网盘成片使用movie_manager.py finish。有固定种子的影片仍需分片检查，不能绕过已失败的分片校验。跨盘硬链接不支持，不自动复制整部视频。
